# Ubuntu Server Installation — Nifty Short Straddle

Step-by-step guide to deploy the Nifty Short Straddle strategy on an Ubuntu server.

---

## 1. Server Prerequisites

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install required packages
sudo apt install -y git python3 python3-venv python3-pip
```

## 2. Clone the Repository

```bash
# Create project directory
mkdir -p ~/algo_trading && cd ~/algo_trading

# Clone the repo
git clone https://github.com/RupanMayan/Strategy_AlgoTrading.git

# Navigate to strategy folder
cd Strategy_AlgoTrading/PythonScript/Options/Nifty_ShortStraddle
```

### Pull Latest Updates (existing install)

```bash
cd ~/algo_trading/Strategy_AlgoTrading
git pull origin main
cd PythonScript/Options/Nifty_ShortStraddle
```

## 3. Configure Secrets

```bash
# Create .env from template
cp .env.example .env

# Edit with your API keys
nano .env
```

Set the following values in `.env`:
```
OPENALGO_APIKEY=your_openalgo_api_key
OPENALGO_USERNAME=your_openalgo_login_username
```

```bash
# Secure the file
chmod 600 .env
```

## 4. Review Strategy Config

```bash
nano config.toml
```

## 5. Automated Setup (Recommended)

```bash
chmod +x setup_service.sh
sudo ./setup_service.sh
```

This will:
- Create a Python virtual environment (`venv/`)
- Install all dependencies from `requirements.txt`
- Set up and enable the systemd service

## 5b. Manual Setup (Alternative)

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Test run
python main.py
```

---

## Service Commands

| Command | Description |
|---------|-------------|
| `sudo systemctl start nifty-straddle` | Start the strategy |
| `sudo systemctl stop nifty-straddle` | Stop gracefully (sends SIGTERM) |
| `sudo systemctl restart nifty-straddle` | Restart the strategy |
| `sudo systemctl status nifty-straddle` | Check if running |
| `sudo systemctl enable nifty-straddle` | Auto-start on boot |
| `sudo systemctl disable nifty-straddle` | Disable auto-start |

## Viewing Logs

```bash
# Tail live logs
journalctl -u nifty-straddle -f

# View today's logs
journalctl -u nifty-straddle --since today

# View last 100 lines
journalctl -u nifty-straddle -n 100
```

## Updating the Strategy

```bash
cd ~/algo_trading/Strategy_AlgoTrading/PythonScript/Options/Nifty_ShortStraddle

# Pull latest code and install/upgrade dependencies in one step
./update.sh

# Restart the service
sudo systemctl restart nifty-straddle
```

## Uninstall

```bash
sudo systemctl stop nifty-straddle
sudo systemctl disable nifty-straddle
sudo rm /etc/systemd/system/nifty-straddle.service
sudo systemctl daemon-reload
```

---

## Diagnostic Commands

```bash
cd ~/algo_trading/Strategy_AlgoTrading/PythonScript/Options/Nifty_ShortStraddle
source venv/bin/activate

# Uncomment one at a time in main.py, then run: python main.py
# strategy.check_connection()       — verify OpenAlgo + show funds
# strategy.check_margin_now()       — test margin guard without trading
# strategy.manual_entry()           — force entry now (all filters run)
# strategy.manual_exit()            — close all active legs immediately
# strategy.show_state()             — dump state + SL levels + DTE info
# strategy.manual_bootstrap_vix()   — fetch/refresh vix_history.csv from NSE
```

## File Reference

| File | Purpose |
|------|---------|
| `config.toml` | Strategy parameters (timing, risk, filters) |
| `.env` | Secrets (API key, Telegram credentials) |
| `.env.example` | Template for `.env` |
| `requirements.txt` | Python dependencies |
| `setup_service.sh` | Automated service installer |
| `update.sh` | Pull code + install/upgrade dependencies |
