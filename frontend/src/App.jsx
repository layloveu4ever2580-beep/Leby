import { useState, useEffect, useCallback } from 'react';
import { Settings, RefreshCw, DollarSign, TrendingUp, Activity, Waves, Loader2 } from 'lucide-react';
import './App.css';

const API_BASE_URL = import.meta.env.VITE_API_URL || '';

function App() {
  const [trades, setTrades] = useState([]);
  const [settings, setSettings] = useState({ targetProfit: 100, theme: 'light', timezone: 'UTC' });
  const [filter, setFilter] = useState('All');
  const [lastSync, setLastSync] = useState(new Date().toLocaleTimeString());
  const [showSettings, setShowSettings] = useState(false);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState(null);

  const fetchTrades = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE_URL}/api/trades`);
      if (!res.ok) throw new Error(`Failed to fetch trades (${res.status})`);
      const data = await res.json();
      setTrades(data);
      setLastSync(new Date().toLocaleTimeString());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchSettings = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/settings`);
      if (!res.ok) return;
      const data = await res.json();
      if (data) setSettings(data);
    } catch (err) {
      console.error('Failed to fetch settings', err);
    }
  }, []);

  useEffect(() => {
    fetchTrades();
    fetchSettings();
  }, [fetchTrades, fetchSettings]);

  const syncTrades = async () => {
    setSyncing(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE_URL}/api/sync-trades`, { method: 'POST' });
      if (!res.ok) throw new Error(`Sync failed (${res.status})`);
      await fetchTrades();
    } catch (err) {
      setError(err.message);
    } finally {
      setSyncing(false);
    }
  };

  const toggleTheme = async () => {
    const newTheme = settings.theme === 'light' ? 'dark' : 'light';
    const updated = { ...settings, theme: newTheme };
    setSettings(updated);
    try {
      await fetch(`${API_BASE_URL}/api/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updated),
      });
    } catch (err) {
      console.error('Failed to persist theme', err);
    }
  };

  const updateSettings = async (e) => {
    e.preventDefault();
    try {
      const res = await fetch(`${API_BASE_URL}/api/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings),
      });
      if (!res.ok) throw new Error('Failed to save settings');
      setShowSettings(false);
    } catch (err) {
      setError(err.message);
    }
  };

  const formatTime = (timestamp) => {
    if (!timestamp) return '—';
    const date = new Date(timestamp);
    return date.toLocaleString();
  };

  // Derived stats
  const totalPnl = trades.reduce((sum, t) => sum + (t.pnl || 0), 0);
  const now = new Date();
  const thisMonthTrades = trades.filter(t => {
    if (!t.timestamp) return false;
    const d = new Date(t.timestamp);
    return d.getMonth() === now.getMonth() && d.getFullYear() === now.getFullYear();
  });
  const thisMonthPnl = thisMonthTrades.reduce((sum, t) => sum + (t.pnl || 0), 0);
  const closedTrades = trades.filter(t => t.status === 'Closed');
  const wins = closedTrades.filter(t => t.pnl > 0).length;
  const losses = closedTrades.filter(t => t.pnl <= 0).length;
  const winRate = closedTrades.length > 0 ? ((wins / closedTrades.length) * 100).toFixed(1) : '0.0';
  const activePositions = trades.filter(t => t.status === 'Open').length;

  const filteredTrades = trades.filter(t => {
    if (filter === 'All') return true;
    return t.status.toLowerCase() === filter.toLowerCase();
  });

  return (
    <div className={`app-container ${settings.theme || 'light'}`}>
      <header className="page-header">
        <div>
          <h1 className="logo-text">Bybit Money Management Bot</h1>
          <p className="subtitle">Position sizing & trade monitoring</p>
        </div>
        <div className="header-actions">
          <div className="connection-status">
            <span className="dot connected"></span> Connected
          </div>
          <button className="theme-toggle" onClick={toggleTheme} aria-label="Toggle theme">
            {settings.theme === 'dark' ? '🌙' : '☀️'}
          </button>
          <button className="btn-settings" onClick={() => setShowSettings(!showSettings)}>
            <Settings size={18} /> Settings
          </button>
        </div>
      </header>

      {error && (
        <div className="error-banner" role="alert">
          {error}
          <button className="error-dismiss" onClick={() => setError(null)} aria-label="Dismiss error">✕</button>
        </div>
      )}

      {showSettings && (
        <section className="settings-panel card">
          <h2>Bot Settings</h2>
          <form onSubmit={updateSettings} className="settings-form">
            <div className="form-group">
              <label htmlFor="targetProfit">Target Profit ($)</label>
              <input
                id="targetProfit"
                type="number"
                value={settings.targetProfit}
                onChange={(e) => setSettings({ ...settings, targetProfit: parseFloat(e.target.value) })}
              />
            </div>
            <button type="submit" className="btn-save">Save Settings</button>
          </form>
        </section>
      )}

      <main className="dashboard-content">
        <div className="stats-row">
          <div className="stat-card">
            <div>
              <h3>Total PnL</h3>
              <div className={`stat-value ${totalPnl >= 0 ? 'positive' : 'negative'}`}>
                ${totalPnl.toFixed(2)}
              </div>
            </div>
            <div className="icon-wrapper blue-icon"><DollarSign size={24} /></div>
          </div>

          <div className="stat-card">
            <div>
              <h3>This Month</h3>
              <div className={`stat-value ${thisMonthPnl >= 0 ? 'positive' : 'negative'}`}>
                ${thisMonthPnl.toFixed(2)}
              </div>
              <p className="sub-text">{thisMonthTrades.length} trades</p>
            </div>
            <div className="icon-wrapper green-icon"><TrendingUp size={24} /></div>
          </div>

          <div className="stat-card">
            <div>
              <h3>Win Rate</h3>
              <div className="stat-value neutral">{winRate}%</div>
              <p className="sub-text">{wins}W / {losses}L</p>
            </div>
            <div className="icon-wrapper purple-icon"><Activity size={24} /></div>
          </div>

          <div className="stat-card">
            <div>
              <h3>Active Positions</h3>
              <div className="stat-value neutral">{activePositions}</div>
              <p className="sub-text">{trades.length} total trades</p>
            </div>
            <div className="icon-wrapper blue-icon"><Waves size={24} /></div>
          </div>
        </div>

        <section className="history-section card">
          <div className="history-header">
            <h2>Trade History</h2>
            <div className="history-actions">
              <div className="tabs" role="tablist">
                {['All', 'Open', 'Closed', 'Failed'].map(tab => (
                  <button
                    key={tab}
                    role="tab"
                    aria-selected={filter === tab}
                    className={`tab-btn ${filter === tab ? 'active' : ''}`}
                    onClick={() => setFilter(tab)}
                  >
                    {tab}
                  </button>
                ))}
              </div>
              <span className="sync-time">Synced {lastSync}</span>
              <button className="btn-sync" onClick={syncTrades} disabled={syncing}>
                {syncing ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />} Sync
              </button>
            </div>
          </div>

          {loading ? (
            <div className="loading-state"><Loader2 size={24} className="spin" /> Loading trades...</div>
          ) : (
            <div className="table-responsive">
              <table className="trades-table">
                <thead>
                  <tr>
                    <th>TICKER</th>
                    <th>ACTION</th>
                    <th>ENTRY</th>
                    <th>TP / SL</th>
                    <th>TARGET $</th>
                    <th>QTY</th>
                    <th>LEV</th>
                    <th>PNL</th>
                    <th>STATUS</th>
                    <th>TIME</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredTrades.map((t, idx) => (
                    <tr key={t.id || idx}>
                      <td className="font-medium">{t.ticker}</td>
                      <td>
                        <span className={`badge ${t.side?.toLowerCase() === 'buy' ? 'bg-green' : 'bg-red'}`}>
                          {t.side?.toUpperCase() || 'BUY'}
                        </span>
                      </td>
                      <td>{t.entry}</td>
                      <td>{t.tp} / {t.sl}</td>
                      <td>${settings.targetProfit}</td>
                      <td>{t.quantity?.toFixed(4)}</td>
                      <td>{t.leverage || '—'}x</td>
                      <td className={t.pnl >= 0 ? (t.pnl === 0 ? 'text-neutral' : 'text-green') : 'text-red'}>
                        ${t.pnl?.toFixed(2) ?? '0.00'}
                      </td>
                      <td>
                        <span className={`status-text ${t.status?.toLowerCase()}`}>{t.status}</span>
                      </td>
                      <td className="text-sm text-gray">{formatTime(t.timestamp)}</td>
                    </tr>
                  ))}
                  {filteredTrades.length === 0 && (
                    <tr>
                      <td colSpan="10" className="empty-state">No trades found.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

export default App;
