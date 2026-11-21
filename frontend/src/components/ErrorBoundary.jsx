import React from 'react';

/**
 * Catches rendering errors anywhere below it in the tree and shows a
 * recoverable fallback instead of an unstyled blank page. In production
 * this is usually where you'd also report the error to a monitoring
 * service (e.g. Sentry) - see the componentDidCatch note below.
 */
class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, info) {
    // eslint-disable-next-line no-console
    console.error('MediFlow UI crashed:', error, info);
    // Hook up an error-reporting service here in production, e.g.:
    // Sentry.captureException(error, { extra: info });
  }

  handleReload = () => {
    this.setState({ hasError: false, error: null });
    window.location.reload();
  };

  render() {
    if (this.state.hasError) {
      return (
        <div
          style={{
            minHeight: '100vh',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '12px',
            padding: '24px',
            textAlign: 'center',
            fontFamily: 'system-ui, sans-serif',
            color: '#1f2937',
            background: '#f8fafc',
          }}
        >
          <h1 style={{ fontSize: '20px', fontWeight: 600 }}>
            Something went wrong
          </h1>
          <p style={{ color: '#6b7280', maxWidth: '420px' }}>
            The MediFlow interface hit an unexpected error. Your data on the
            server is safe - reloading usually fixes this.
          </p>
          <button
            onClick={this.handleReload}
            style={{
              padding: '8px 16px',
              borderRadius: '8px',
              border: 'none',
              background: '#3B82F6',
              color: 'white',
              fontWeight: 500,
              cursor: 'pointer',
            }}
          >
            Reload
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}

export default ErrorBoundary;
