# NYC Heat Index (Resy Hit List + Eater Heatmap) — Free, Fully Automated

This repo generates a **monthly, minimal** newsletter that:
- pulls **Resy Hit List (NYC)** + **Eater Heatmap (NYC / Manhattan)**
- extracts restaurants + blurbs
- dedupes across sources
- computes a **True Heat Score (0–100)**
- adds **reservation difficulty + booking tips**
- generates a clean HTML issue
- **emails it automatically** on the 1st of every month via GitHub Actions

## 1) Create your newsletter "archive" (free, shareable)
This repo can publish each issue HTML to **GitHub Pages** so every issue has a shareable URL.

1. In GitHub: **Settings → Pages**
2. Source: **Deploy from a branch**
3. Branch: **gh-pages** (the workflow will create it)

## 2) Set up email sending (free)
This uses SMTP. The easiest free option is Gmail.

### Gmail prerequisites
- Turn on **2‑Step Verification**
- Create an **App Password** (Google Account → Security → App passwords)
- Use that app password below (NOT your normal password)

## 3) Add GitHub Secrets
In GitHub: **Settings → Secrets and variables → Actions → New repository secret**

Required:
- `SMTP_HOST` = `smtp.gmail.com`
- `SMTP_PORT` = `587`
- `SMTP_USERNAME` = your Gmail address (the sender)
- `SMTP_PASSWORD` = your Gmail **app password**
- `FROM_EMAIL` = same as SMTP_USERNAME (or whatever you want as From)
- `FROM_NAME` = e.g. `NYC Heat Index`
- `SUBSCRIBERS` = comma-separated emails (e.g. `a@x.com,b@y.com`)
- `TIMEZONE` = `America/New_York`

Optional:
- `REPLY_TO` = email address for replies
- `DRY_RUN` = `true` to test without sending (still generates output)

## 4) Run it once (test)
- Go to **Actions → Monthly NYC Heat Index → Run workflow**
- Check the action logs + the generated issue URL (GitHub Pages)

## 5) Customize sources
Edit `src/config.py`. By default it uses:
- Resy Hit List NYC
- Eater Manhattan Heatmap

You can add Brooklyn/Queens heatmaps by pasting the URLs.

---

# Notes / Limitations
- These sites can change HTML structure. The parser is written to be resilient, but you may need to tweak selectors.
- “Reservation difficulty” is heuristic. If you later want true availability checks, you can add Resy/OpenTable lookups.
