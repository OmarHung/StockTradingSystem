# VPS 部署指南（Ubuntu）

架構：uvicorn 只綁 `127.0.0.1`，**不開任何公網 port**，一律透過 SSH tunnel 存取 WebUI。
排程（盤中監控／資料更新／每日主流程）跑在 uvicorn 進程內，由 systemd 常駐。

適用：Ubuntu 22.04 / 24.04。以下以使用者 `<USER>`、安裝路徑 `/home/<USER>/StockTradingSystem` 為例。

---

## 1. 系統前置

```bash
# 時區必須是台北，否則排程時間（09:00/14:30/15:00）全部跑錯
sudo timedatectl set-timezone Asia/Taipei

# 防火牆：只開 SSH
sudo ufw default deny incoming
sudo ufw allow OpenSSH
sudo ufw enable

# SSH 只允許金鑰登入（確認本機金鑰已能登入後再做）
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

安裝套件（Ubuntu 24.04 內建 Python 3.12；22.04 請先加 deadsnakes PPA 裝 python3.12）：

```bash
sudo apt update
sudo apt install -y python3.12-venv python3.12-dev git build-essential

# Node 20（build 前端用）
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

## 2. 取得程式碼與安裝

```bash
git clone <repo-url> ~/StockTradingSystem
cd ~/StockTradingSystem

python3.12 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt   # Linux 不需要 Mac 的 arch -arm64 前綴

# build 前端（FastAPI 偵測到 frontend/dist 會自動掛載，單一 port 同源）
cd frontend && npm ci && npm run build && cd ..
```

## 3. 搬移私密資料（不在 git 內，必須手動帶上去）

在**本機**執行：

```bash
scp .env <USER>@<VPS_IP>:~/StockTradingSystem/.env
rsync -av data/market.db data/chroma <USER>@<VPS_IP>:~/StockTradingSystem/data/
```

`.env` 需要的鍵（缺哪個對應功能就停用）：

| 鍵 | 用途 |
|---|---|
| `ANTHROPIC_API_KEY` | LLM 分析／決策（必要） |
| `FINMIND_TOKEN` | FinMind 資料備援（免費匿名額度小，建議放） |
| `SJ_API_KEY` / `SJ_SEC_KEY` | 永豐 shioaji 行情與交易 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 每日決策報告推播 |

shioaji 若要跑**正式環境**（Phase 5），另需把憑證 `.pfx` 檔帶上去，路徑與 `.env` 設定對齊。

## 4. systemd 常駐

`sudo tee /etc/systemd/system/trading.service`：

```ini
[Unit]
Description=StockTradingSystem WebUI + Scheduler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<USER>
WorkingDirectory=/home/<USER>/StockTradingSystem
Environment=TZ=Asia/Taipei
ExecStart=/home/<USER>/StockTradingSystem/.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
# 讓部署使用者不用 sudo 就能看 service log（重新登入生效）
sudo usermod -aG systemd-journal <USER>

sudo systemctl daemon-reload
sudo systemctl enable --now trading
systemctl status trading            # 應為 active (running)
curl -s localhost:8000/api/scheduler/status   # 排程狀態應有回應
journalctl -u trading -f            # 看即時 log
```

注意：`--host 127.0.0.1` 是安全設計的一部分，**不要**改成 `0.0.0.0`。

## 5. 本機存取（SSH tunnel）

一次性連線直接下指令即可（`-o` 兩項為 keepalive，防 NAT 靜默斷線）：

```bash
ssh -N -L 8000:127.0.0.1:8000 \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 <USER>@<VPS_IP>
```

常用的話建議存成別名——本機 `~/.ssh/config` 加入：

```
Host trading
    HostName <VPS_IP>
    User <USER>
    LocalForward 8000 127.0.0.1:8000
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

之後：

```bash
ssh -N trading        # 掛著這個連線
```

瀏覽器開 `http://localhost:8000` 即為 WebUI（前端已由 FastAPI 同源服務，無 CORS 問題）。
斷線自動重連可改用 `autossh -M 0 -N trading`（`brew install autossh`）。

## 6. 更新部署

```bash
cd ~/StockTradingSystem
git pull
.venv/bin/pip install -r requirements.txt
cd frontend && npm ci && npm run build && cd ..
sudo systemctl restart trading
```

避開排程時段（平日 09:00–15:30）重啟；`journalctl -u trading -n 50` 確認啟動無誤。

## 7. 備份

要備的只有三樣：`data/market.db`、`data/chroma/`（反思記憶）、`.env`。
最簡做法——在**本機** crontab 定期拉回：

```
0 22 * * 1-5 rsync -a trading:~/StockTradingSystem/data/ ~/backup/trading-data/
```

（`market.db` 可隨時重新回補，`chroma` 的交易經驗與反思規則不可再生，優先保。）

## 8. 收集除錯資訊（丟給 Claude 用）

Log 有兩層，除錯時兩個都抓：

- **journald**：uvicorn 啟動訊息、存取記錄、崩潰時的 stderr。
- **`logs/system.log`**：應用層 log（排程、選股、LLM、回補），含毫秒時間戳、自動輪替（10MB×5）；未捕捉例外的完整 traceback 也在這。API 錯誤（4xx/5xx）與慢請求會標記進來。
- **`logs/jobs/*.log`**：各背景任務（daily/backfill/intraday/backtest）子行程的完整輸出。

一鍵打包（在 VPS 上跑，產出單一檔案丟給 Claude）：

```bash
cd ~/StockTradingSystem
{ echo "=== journalctl (最近 200 行) ==="; journalctl -u trading -n 200 --no-pager
  echo; echo "=== system.log (最近 300 行) ==="; tail -300 logs/system.log
  for f in logs/jobs/*.log; do echo; echo "=== $f (最近 100 行) ==="; tail -100 "$f"; done
} > /tmp/trading-debug.txt
```

再從本機拉回：`scp trading:/tmp/trading-debug.txt .`

不想 SSH 的話，透過 tunnel 直接用 API 撈（`name` 可為 `system` 或任一 job 名）：

```bash
curl -s 'localhost:8000/api/logs?name=system&tail=300' | python3 -m json.tool
```

想看更細的 log（含每個 API 請求）：WebUI 設定或 `config/settings.yaml` 把 `logging.level` 改成 `DEBUG` 後重啟。

## 9. 疑難排解

- **WebUI 開不起來**：`journalctl -u trading -n 100`；確認 `frontend/dist` 存在（沒 build 就只有 `/api/*` 能用）。
- **排程沒跑**：`date` 確認時區；WebUI 設定 → 排程 檢查啟用狀態；`/api/scheduler/status`。
- **chromadb / tenant 錯誤**：多半是 `data/chroma` 沒搬完整或權限不對；確認目錄擁有者為 `<USER>`。
- **LLM 呼叫失敗**：`.env` 的 `ANTHROPIC_API_KEY` 是否存在且非空字串（空字串會讓 SDK 壞掉，程式已有防護但金鑰仍需有效）。
