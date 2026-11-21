from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from models import Appointment, Doctor


def parse_time(time_str: str) -> datetime:
    try:
        return datetime.strptime(time_str, "%Y-%m-%d %H:%M")
    except ValueError:
        # Fallback in case of seconds
        return datetime.strptime(time_str[:16], "%Y-%m-%d %H:%M")

def format_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def preview_appointment_slot(
    db: Session,
    doctor_id: str,
    preferred_time_str: str,
    duration_mins: int,
) -> dict:
    """Return the first non-conflicting slot without changing the database.

    This is deliberately separate from ``calculate_appointment_slot``.  A
    recommendation must not increment a doctor's queue, occupy a hospital bed,
    or move another patient's appointment until the patient actually books.
    """
    preferred_start = parse_time(preferred_time_str)
    date_str = preferred_start.strftime("%Y-%m-%d")

    doctor = db.query(Doctor).filter(Doctor.doctor_id == doctor_id).first()
    if not doctor:
        raise ValueError(f"Doctor {doctor_id} not found")

    active_appointments = db.query(Appointment).filter(
        Appointment.doctor_id == doctor_id,
        Appointment.appointment_time.like(f"{date_str}%"),
        Appointment.status != "CANCELLED",
    ).all()
    active_appointments.sort(key=lambda appointment: parse_time(appointment.appointment_time))

    slot_start = preferred_start
    while True:
        slot_end = slot_start + timedelta(minutes=duration_mins)
        conflicting_appointment = next(
            (
                appointment
                for appointment in active_appointments
                if not (
                    slot_end <= parse_time(appointment.appointment_time)
                    or slot_start >= parse_time(appointment.appointment_time)
                    + timedelta(minutes=appointment.estimated_duration)
                )
            ),
            None,
        )
        if not conflicting_appointment:
            break

        slot_start = parse_time(conflicting_appointment.appointment_time) + timedelta(
            minutes=conflicting_appointment.estimated_duration
        )

    return {
        "scheduled_time": format_time(slot_start),
        "wait_minutes": int((slot_start - preferred_start).total_seconds() // 60),
    }

def calculate_appointment_slot(
    db: Session,
    doctor_id: str,
    priority: str,
    preferred_time_str: str,
    duration_mins: int
) -> dict:
    """
    Schedules an appointment based on patient priority:
    - LOW/MEDIUM: Find next available slot that doesn't conflict, starting from preferred_time.
    - HIGH/EMERGENCY: Place immediately at preferred_time, shift subsequent appointments.
    """
    pref_dt = parse_time(preferred_time_str)
    date_str = pref_dt.strftime("%Y-%m-%d")

    # Fetch doctor working hours and consult length
    doc = db.query(Doctor).filter(Doctor.doctor_id == doctor_id).first()
    if not doc:
        raise ValueError(f"Doctor {doctor_id} not found")

    # Fetch active appointments for doctor on this day
    active_apps = db.query(Appointment).filter(
        Appointment.doctor_id == doctor_id,
        Appointment.appointment_time.like(f"{date_str}%"),
        Appointment.status != "CANCELLED"
    ).all()

    # Sort appointments by time
    active_apps.sort(key=lambda x: parse_time(x.appointment_time))

    if priority in ("HIGH", "EMERGENCY"):
        # Shift scheduling: insert at preferred_time, push subsequent
        # appointments forward.
        #
        # IMPORTANT: each displaced appointment is pushed past every
        # slot already reserved ahead of it (the new emergency slot,
        # plus every other appointment already shifted in this same
        # pass) rather than by a fixed, independent offset. The
        # previous version shifted each appointment by a flat
        # `duration_mins` computed from its own original start time -
        # which can land two different appointments (or a shifted one
        # and an untouched one) on the exact same slot when the
        # doctor's existing schedule wasn't uniformly spaced. That's a
        # real double-booking bug, not just an edge case: it's caught
        # here by processing appointments in time order and tracking
        # reserved intervals cumulatively.
        new_app_start = pref_dt
        new_app_end = new_app_start + timedelta(minutes=duration_mins)

        reserved_intervals = [(new_app_start, new_app_end)]
        pending_moves = []  # (app, old_time, candidate_start, note)

        apps_to_shift = sorted(
            (a for a in active_apps if parse_time(a.appointment_time) >= new_app_start),
            key=lambda a: parse_time(a.appointment_time),
        )

        for app in apps_to_shift:
            original_start = parse_time(app.appointment_time)
            app_duration = app.estimated_duration
            candidate_start = original_start

            # Push past any interval already reserved (the new
            # appointment, or an earlier appointment already shifted
            # in this same loop) that this one would otherwise overlap.
            while True:
                candidate_end = candidate_start + timedelta(minutes=app_duration)
                overlap = next(
                    (
                        r for r in reserved_intervals
                        if not (candidate_end <= r[0] or candidate_start >= r[1])
                    ),
                    None,
                )
                if not overlap:
                    break
                candidate_start = overlap[1]

            reserved_intervals.append(
                (candidate_start, candidate_start + timedelta(minutes=app_duration))
            )

            if candidate_start != original_start:
                note = f"Delayed due to priority patient {priority} scheduled at {format_time(new_app_start)}."
                pending_moves.append((app, app.appointment_time, candidate_start, note))

        # Apply the moves in two passes rather than directly in the loop
        # above. Reason: the final set of new times is guaranteed
        # collision-free (that's what reserved_intervals enforces), but
        # the UNIQUE(doctor_id, appointment_time) index is checked
        # immediately per UPDATE statement, not deferred to commit. If
        # appointment A's new slot happens to equal appointment B's
        # OLD (not-yet-updated) slot, writing A first trips the
        # constraint even though the end state is fine. Parking every
        # moving appointment at a guaranteed-unique placeholder first
        # sidesteps needing to work out a collision-free write order.
        for i, (app, _old_time, _new_start, _note) in enumerate(pending_moves):
            app.appointment_time = format_time(datetime(2099, 1, 1) + timedelta(minutes=i))
        if pending_moves:
            db.flush()

        shifted_logs = []
        for app, old_time, candidate_start, note in pending_moves:
            new_time_str = format_time(candidate_start)
            app.appointment_time = new_time_str
            app.status = "SHIFTED"
            app.notes = note
            shifted_logs.append({
                "appointment_id": app.appointment_id,
                "patient_name": app.patient_name,
                "old_time": old_time,
                "new_time": new_time_str,
                "reason": note,
            })

        # Save changes to the DB
        db.commit()

        # Update Doctor queue and workload
        doc.queue_count = len(active_apps) + 1
        doc.workload = min(100.0, round(((len(active_apps) + 1) * doc.consultation_duration / 480.0) * 100, 1))

        # Increment hospital occupancy
        if doc.hospital:
            doc.hospital.current_occupancy = min(doc.hospital.total_capacity, doc.hospital.current_occupancy + 1)
        db.commit()

        return {
            "scheduled_time": format_time(new_app_start),
            "shifted_appointments": shifted_logs,
            "status": "BOOKED",
            "notes": f"Immediate scheduling activated for priority level {priority}."
        }

    else:
        # Standard scheduling: find next free slot
        new_app_start = pref_dt

        while True:
            conflict = False
            new_app_end = new_app_start + timedelta(minutes=duration_mins)

            for app in active_apps:
                app_start = parse_time(app.appointment_time)
                app_end = app_start + timedelta(minutes=app.estimated_duration)

                # Check overlapping conditions
                if not (new_app_end <= app_start or new_app_start >= app_end):
                    conflict = True
                    # Push start time to end of conflicting appointment
                    new_app_start = app_end
                    break

            if not conflict:
                break

        scheduled_time_str = format_time(new_app_start)

        # Update Doctor queue
        doc.queue_count = len(active_apps) + 1
        doc.workload = min(100.0, round(((len(active_apps) + 1) * doc.consultation_duration / 480.0) * 100, 1))

        # Increment hospital occupancy
        if doc.hospital:
            doc.hospital.current_occupancy = min(doc.hospital.total_capacity, doc.hospital.current_occupancy + 1)
        db.commit()

        return {
            "scheduled_time": scheduled_time_str,
            "shifted_appointments": [],
            "status": "BOOKED",
            "notes": "Scheduled at first available time slot."
        }


def cancel_appointment_and_pull_forward(db: Session, appointment_id: str) -> dict:
    """
    Cancels an appointment and pulls forward subsequent appointments on that day
    for the same doctor to fill the gap, reducing wait times.
    """
    app_to_cancel = db.query(Appointment).filter(Appointment.appointment_id == appointment_id).first()
    if not app_to_cancel:
        raise ValueError(f"Appointment {appointment_id} not found")

    if app_to_cancel.status == "CANCELLED":
        return {"message": "Appointment already cancelled", "shifted_appointments": []}

    cancelled_time_str = app_to_cancel.appointment_time
    cancelled_dt = parse_time(cancelled_time_str)
    date_str = cancelled_dt.strftime("%Y-%m-%d")
    doctor_id = app_to_cancel.doctor_id

    # Mark as cancelled
    app_to_cancel.status = "CANCELLED"
    app_to_cancel.notes = f"Cancelled on {datetime.now().strftime('%Y-%m-%d %H:%M')}."

    # Decrement hospital occupancy
    doc = db.query(Doctor).filter(Doctor.doctor_id == doctor_id).first()
    if doc:
        if doc.hospital and doc.hospital.current_occupancy > 0:
            doc.hospital.current_occupancy = max(0, doc.hospital.current_occupancy - 1)

    # Fetch subsequent active appointments for this doctor on this day
    subsequent_apps = db.query(Appointment).filter(
        Appointment.doctor_id == doctor_id,
        Appointment.appointment_time.like(f"{date_str}%"),
        Appointment.status != "CANCELLED",
        Appointment.appointment_id != appointment_id
    ).all()

    # Filter subsequent ones in memory
    subsequent_apps = [a for a in subsequent_apps if parse_time(a.appointment_time) > cancelled_dt]
    subsequent_apps.sort(key=lambda x: parse_time(x.appointment_time))

    # Pack subsequent appointments back-to-back starting from the freed
    # slot, using each appointment's OWN duration (not the cancelled
    # appointment's) - the previous version pulled every appointment
    # earlier by a fixed offset equal to the cancelled appointment's
    # duration regardless of the moving appointment's own length, which
    # both under- and over-corrects whenever durations differ.
    reserved_start = cancelled_dt
    pending_moves = []  # (app, old_time, candidate_start, note)
    for app in subsequent_apps:
        app_duration = app.estimated_duration
        candidate_start = reserved_start
        reserved_start = candidate_start + timedelta(minutes=app_duration)

        if candidate_start != parse_time(app.appointment_time):
            note = f"Pulled forward due to cancellation of appointment at {cancelled_time_str}."
            pending_moves.append((app, app.appointment_time, candidate_start, note))

    # Same two-phase "park then place" write as calculate_appointment_slot
    # above - avoids the UNIQUE(doctor_id, appointment_time) index
    # rejecting a mid-transaction state where one appointment's new time
    # transiently equals another's not-yet-updated old time.
    for i, (app, _old_time, _new_start, _note) in enumerate(pending_moves):
        app.appointment_time = format_time(datetime(2099, 1, 1) + timedelta(minutes=i))
    if pending_moves:
        db.flush()

    shifted_logs = []
    for app, old_time, candidate_start, note in pending_moves:
        new_time_str = format_time(candidate_start)
        app.appointment_time = new_time_str
        app.status = "SHIFTED"
        app.notes = note
        shifted_logs.append({
            "appointment_id": app.appointment_id,
            "patient_name": app.patient_name,
            "old_time": old_time,
            "new_time": new_time_str,
            "reason": note,
        })

    # Update doctor queue count and workload
    active_count = db.query(Appointment).filter(
        Appointment.doctor_id == doctor_id,
        Appointment.appointment_time.like(f"{date_str}%"),
        Appointment.status != "CANCELLED"
    ).count()

    if doc:
        doc.queue_count = active_count
        doc.workload = min(100.0, round((active_count * doc.consultation_duration / 480.0) * 100, 1))

    db.commit()

    return {
        "message": f"Appointment {appointment_id} cancelled.",
        "shifted_appointments": shifted_logs
    }
