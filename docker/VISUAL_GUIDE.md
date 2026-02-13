# 🐳 Docker Setup Complete!

Your Bazarr AutoTranslate project has been successfully containerized!

## 📁 What's in the `docker/` folder?

```
docker/
├── 🔧 Configuration Files
│   ├── .env.example          # Template for your settings
│   ├── docker-compose.yml    # Docker service definition
│   └── Dockerfile           # Container build instructions
│
├── 🐍 Python Scripts
│   ├── Bazarr_AutoTranslate.py  # Main translation script (continuous mode)
│   ├── clean_et_subs.py         # Cleanup script (unchanged)
│   └── requirements.txt         # Python dependencies
│
├── 🚀 Scripts
│   ├── entrypoint.sh         # Container startup script
│   └── build.sh              # Quick build helper
│
└── 📚 Documentation
    ├── README.md             # Full Docker guide
    ├── MIGRATION.md          # Changes explained
    └── QUICKSTART.md         # This file!
```

## 🎯 Key Changes

### Before (Cron)
```
⏰ Runs once per hour
⏱️  50-minute timeout
💤 Sleeps between runs
📝 Config in Python files
```

### After (Docker)
```
🔄 Runs continuously
♾️  No timeouts
🚀 Processes immediately
⚙️  Config in .env file
📦 Everything containerized
```

## 🚦 Quick Start Guide

### Step 1: Configure
```bash
cd docker
cp .env.example .env
nano .env
```

Set these **required** variables:
```env
BAZARR_HOSTNAME=your.bazarr.server:6767
BAZARR_APIKEY=your_api_key_here
MEDIA_PATH=/path/to/your/media
```

### Step 2: Build & Run
```bash
docker-compose up -d
```

### Step 3: Check Logs
```bash
docker-compose logs -f
```

### Step 4: Stop
```bash
docker-compose down
```

## 🔧 Configuration Overview

### Translation Settings
| Variable | Default | Description |
|----------|---------|-------------|
| `FIRST_LANG` | `et` | Primary target language |
| `SECOND_LANG` | `sv` | Secondary target language |
| `CHECK_INTERVAL` | `300` | Seconds between API checks |
| `MAX_PARALLEL_TRANSLATIONS` | `2` | Parallel processing jobs |

### Cleanup Settings
| Variable | Default | Description |
|----------|---------|-------------|
| `CLEANUP_TIME` | `04:00` | Daily cleanup time (HH:MM) |
| `CLEANUP_ROOT` | `/media` | Root directory to scan |
| `CLEANUP_MIN_CONFIDENCE` | `0.70` | Language detection confidence |

## 🎬 How It Works

### Main Loop (Continuous)
```
┌─────────────────────────────────────┐
│ 1. Check Bazarr API                 │
│    ↓                                 │
│ 2. Missing subtitles found?         │
│    ├─ Yes → Process all             │
│    └─ No → Skip                     │
│    ↓                                 │
│ 3. Wait CHECK_INTERVAL seconds      │
│    ↓                                 │
│ 4. Repeat from step 1               │
└─────────────────────────────────────┘
```

### Daily Cleanup (Scheduled)
```
┌─────────────────────────────────────┐
│ At CLEANUP_TIME (04:00 default):    │
│    ↓                                 │
│ 1. Scan .et.srt files               │
│    ↓                                 │
│ 2. Detect actual language           │
│    ↓                                 │
│ 3. Remove non-Estonian files        │
│    ↓                                 │
│ 4. Remove HTTP error files          │
└─────────────────────────────────────┘
```

## 📊 Processing Flow

```
Movie/Episode missing subtitles
         ↓
    Download EN subs
         ↓
    ┌────────────┐
    │ Translate  │
    │ to ET      │
    └────────────┘
         ↓
    ┌────────────┐
    │ Translate  │
    │ to SV      │
    └────────────┘
         ↓
    ✅ Complete
```

## 🎨 Example Configurations

### Aggressive Processing
```env
MAX_PARALLEL_TRANSLATIONS=5
CHECK_INTERVAL=60
TRANSLATE_DELAY=0.1
```
⚡ Fastest processing, higher load

### Balanced (Default)
```env
MAX_PARALLEL_TRANSLATIONS=2
CHECK_INTERVAL=300
TRANSLATE_DELAY=0.3
```
⚖️ Good balance of speed and load

### Conservative
```env
MAX_PARALLEL_TRANSLATIONS=1
CHECK_INTERVAL=600
TRANSLATE_DELAY=1.0
```
🐌 Gentlest on the system

## 🔍 Monitoring

### View All Logs
```bash
docker-compose logs -f
```

### View Specific Logs
```bash
# Main translation logs
docker-compose logs -f bazarr-autotranslate

# Cleanup logs
docker exec bazarr-autotranslate cat /var/log/bazarr-autotranslate/cleanup.log

# Cron logs
docker exec bazarr-autotranslate cat /var/log/bazarr-autotranslate/cron.log
```

### Check Status
```bash
docker-compose ps
```

## 🛠️ Common Tasks

### Restart Container
```bash
docker-compose restart
```

### Update Configuration
```bash
nano .env
docker-compose restart
```

### Rebuild After Code Changes
```bash
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

### Manual Cleanup Run
```bash
docker exec bazarr-autotranslate /app/run_cleanup.sh
```

## ✅ Testing Checklist

- [ ] `.env` file created and configured
- [ ] `BAZARR_HOSTNAME` points to your server
- [ ] `BAZARR_APIKEY` is correct
- [ ] `MEDIA_PATH` mounted correctly
- [ ] Container starts: `docker-compose up -d`
- [ ] Logs show connection to Bazarr
- [ ] Subtitles are being processed
- [ ] Daily cleanup runs at configured time

## 🚨 Troubleshooting

### Container Won't Start
```bash
docker-compose logs
docker-compose config  # Check env vars
```

### Can't Connect to Bazarr
- Check `BAZARR_HOSTNAME` format
- Use container name if on same Docker network
- Verify API key in Bazarr settings

### No Subtitles Being Processed
- Check Bazarr has translation providers configured
- Verify there are actually missing subtitles
- Check API timeout settings

### Cleanup Not Running
```bash
# Check cron logs
docker exec bazarr-autotranslate cat /var/log/bazarr-autotranslate/cron.log
```

## 📝 Notes

- Original Python scripts in parent directory are **unchanged**
- Docker version is **completely separate**
- You can still use cron if needed
- Both versions can coexist (but don't run both!)

## 🎓 Learning Resources

- **README.md** - Complete Docker usage guide
- **MIGRATION.md** - Detailed explanation of all changes
- **QUICKSTART.md** - This file!

## 🎉 You're Done!

Your Bazarr AutoTranslate is now:
- ✅ Containerized
- ✅ Running continuously
- ✅ Easily configurable
- ✅ Auto-restarting
- ✅ Well documented

Just run `docker-compose up -d` and let it handle everything!

---

**Need help?** Check the documentation files in the `docker/` folder or open an issue on GitHub.
