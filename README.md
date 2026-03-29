# OpenClaw - Ghi chep cai dat va bao mat

> Ngay cai dat: 2026-03-16
> Phien ban: OpenClaw 2026.3.13
> Image: ghcr.io/openclaw/openclaw:latest

---

## Tong quan

OpenClaw chay trong Docker tren may picopiece-X99 (Ubuntu, 62GB RAM, 56 cores).
Bot Telegram `@PicoPieceOpenClawBot` su dung DeepSeek (primary) va Google Gemini (fallback) de tra loi tin nhan.

## Cau truc thu muc

```
/home/picopiece/openclaw/
├── docker-compose.yml      # Docker Compose config (da hardened)
├── .env                    # API keys va secrets (chmod 600)
├── backup.sh               # Script backup data (giu 7 ban)
├── setup-firewall.sh       # Script bat ufw (chay voi sudo)
├── README.md               # File nay
└── data/                   # Du lieu OpenClaw (mount volume)
    ├── openclaw.json       # Config chinh
    ├── canvas/
    ├── cron/
    ├── logs/
    └── telegram/
```

## Qui trinh cai dat

1. Tao Telegram Bot qua @BotFather -> lay Bot Token
2. Lay Google Gemini API Key tu https://aistudio.google.com/apikey
3. Tao thu muc `/home/picopiece/openclaw/` va file `.env` (chmod 600) chua credentials
4. Tao `docker-compose.yml` voi cac cau hinh bao mat (xem muc ben duoi)
5. Pull image: `docker compose pull`
6. Khoi dong: `docker compose up -d`
7. Doi model tu Anthropic (mac dinh) sang Gemini:
   ```bash
   docker exec openclaw openclaw models set google/gemini-2.5-flash-lite
   ```
8. Cau hinh Telegram policy (pairing, allowlist)
9. Approve user qua pairing code
10. Bat firewall (ufw) bang script `setup-firewall.sh`

## Cau hinh hien tai

### LLM

- **Model chinh**: `deepseek/deepseek-chat` (DeepSeek V3, 128K context, tra phi theo token)
- **Fallback chain** (tu dong chuyen khi model chinh loi):
  1. `google/gemini-2.5-flash` (free tier)
  2. `google/gemini-2.5-flash-lite` (free tier)
  3. `google/gemini-2.5-pro` (free tier)
- **DeepSeek pricing**: ~$0.27/M input, $1.10/M output (cache hit: $0.07/M)
- Doi model: `docker exec openclaw openclaw models set <model_id>`

### DeepSeek Cost Tracking

- **Script**: `/home/picopiece/openclaw/deepseek_cost_tracker.py`
- **Cron**: Chay moi 4 gio, gui bao cao chi phi len Telegram
- **Canh bao**: Tu dong gui khi balance giam duoi $1.50, $1.00, $0.50, $0.20
- **State file**: `data/deepseek_cost_state.json`
- **Chay thu cong**: `python3 /home/picopiece/openclaw/deepseek_cost_tracker.py`

### Telegram

- **Bot**: `@PicoPieceOpenClawBot`
- **DM policy**: `pairing` -- nguoi dung phai duoc duyet truoc khi chat
- **Group policy**: `allowlist` -- chi group duoc cho phep moi nhan message
- **Streaming**: `partial` -- bot gui tin nhan theo tung phan
- **Session**: `per-channel-peer` -- moi nguoi DM co session rieng biet

### User da duoc approve

- Telegram user ID: `543327059` (Mr.Kokono) -- approve qua pairing code `RRSMSLES`

## Bao mat Docker

| Cau hinh | Gia tri | Muc dich |
|---|---|---|
| ports | `127.0.0.1:18789:18789` | Chi bind localhost, khong expose ra ngoai |
| user | `1000:1000` | Chay non-root (uid picopiece) |
| read_only | `true` | Root filesystem khong ghi duoc |
| security_opt | `no-new-privileges:true` | Khong cho phep leo thang quyen |
| cap_drop | `ALL` | Xoa tat ca Linux capabilities |
| resources.limits | 4 CPU, 4GB RAM | Gioi han tai nguyen |
| tmpfs /tmp | `noexec,nosuid,512M` | /tmp tam thoi, khong chay binary |
| docker.sock | **KHONG mount** | Tranh full host compromise |
| logging | json-file, max 10MB x 3 | Tu dong rotate log |

## Firewall (ufw)

Da bat ufw voi cac rule:

| Port | Service | Cho phep |
|---|---|---|
| 22 | SSH | Co |
| 3000 | Grafana | Co |
| 8000, 8002, 8003 | xiaozhi-server | Co |
| 8005 | parent-dashboard | Co |
| 8080 | Jenkins | Co |
| 9090 | Prometheus | Co |
| 9101 | Python monitor | Co |
| **18789** | **OpenClaw** | **KHONG** (localhost only) |

## Gateway

- **Auth mode**: token
- **Token**: luu trong `data/openclaw.json` (truong `gateway.auth.token`)
- **Dashboard**: `http://127.0.0.1:18789/` (chi truy cap tu may local)
- **Truy cap tu xa**: dung SSH tunnel:
  ```bash
  ssh -L 18789:127.0.0.1:18789 picopiece@<server_ip>
  ```

## Cac lenh thuong dung

```bash
# Xem trang thai container
docker ps --filter name=openclaw

# Xem logs (realtime)
docker logs openclaw -f --tail 50

# Restart
docker compose restart openclaw

# Xem config
docker exec openclaw cat /home/node/.openclaw/openclaw.json

# Doi model
docker exec openclaw openclaw models set google/gemini-2.5-flash

# Xem trang thai model
docker exec openclaw openclaw models status

# Approve nguoi dung moi (khi ho gui DM va nhan pairing code)
docker exec openclaw openclaw pairing approve telegram <PAIRING_CODE>

# Xem danh sach thiet bi / nguoi dung
docker exec openclaw openclaw devices list

# Chay security audit
docker exec openclaw openclaw security audit --deep

# Backup du lieu
/home/picopiece/openclaw/backup.sh

# Cap nhat image
docker compose pull && docker compose up -d
```

## Backup

- Script: `/home/picopiece/openclaw/backup.sh`
- Luu tai: `/home/picopiece/backup/openclaw/`
- Giu toi da 7 ban, tu xoa ban cu
- Them vao crontab de tu dong chay hang ngay:
  ```bash
  crontab -e
  # Them dong:
  0 2 * * * /home/picopiece/openclaw/backup.sh >> /home/picopiece/openclaw/backup.log 2>&1
  ```

## Luu y bao mat

- **KHONG** expose port 18789 ra public internet
- **KHONG** mount docker.sock vao container
- **KHONG** luu API key o noi nao khac ngoai file `.env` (chmod 600)
- Dinh ky chay `openclaw security audit --deep` de kiem tra
- Khi approve nguoi dung moi, xac nhan danh tinh truoc khi approve
- Free tier Gemini: du lieu co the duoc Google su dung de train model
- DeepSeek: tra phi theo token, theo doi balance qua cost tracker script
