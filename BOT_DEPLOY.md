# Running the crypto paper bot 24/7 on GitHub Actions

This runs the bot every 30 minutes in GitHub's cloud — so it keeps trading even
when your PC is **off** and TradingView is **closed** (it uses CoinGecko data and
computes the indicators itself). The virtual account state (`advisor.db`) is
committed back to the repo each run, so it persists.

## One-time setup

1. **Create your own GitHub repository** (make it **Private** — recommended).

2. **Push this project to it.** From this folder:
   ```bash
   git init                      # if not already a git repo of your own
   git add .
   git commit -m "crypto paper bot"
   git branch -M main
   git remote add origin https://github.com/<you>/<your-repo>.git
   git push -u origin main
   ```
   (If `origin` already points at the upstream tradingview-mcp repo, replace it:
   `git remote set-url origin https://github.com/<you>/<your-repo>.git`.)

3. **Add the secrets** — repo → **Settings → Secrets and variables → Actions →
   New repository secret** — create:
   | Name | Value |
   |------|-------|
   | `ANTHROPIC_API_KEY` | your (rotated) Anthropic key |
   | `TELEGRAM_BOT_TOKEN` | your (rotated) bot token |
   | `TELEGRAM_CHAT_ID` | `819108399` |

4. **Enable Actions** — repo → **Actions** tab → enable workflows.

5. **Test now** — Actions → *crypto-paper-bot* → **Run workflow**. Within a minute
   you should get a Telegram message and see a new commit updating `advisor.db`.

That's it. It will then run automatically on the schedule.

## Tuning
- **Frequency:** edit `cron: "*/30 * * * *"` in `.github/workflows/bot.yml`
  (e.g. `0 * * * *` = hourly). GitHub cron is UTC and may be delayed a few minutes.
- **Reset the experiment:** Actions → Run workflow won't reset; to restart at
  €1000, run `python btc_bot.py --reset` locally and push, or delete the
  `paper_*` rows in `advisor.db`.

## Notes
- The repo is the source of truth for state. Don't run the local loop and the
  Actions loop against the same repo at the same time, or they'll fight over
  `advisor.db`.
- CoinGecko's free API is occasionally rate-limited from cloud IPs; if a run
  can't fetch prices it simply skips that cycle (no trade), and the next run
  retries.
- Local use is unchanged: open the dashboard / run `python btc_bot.py --once`,
  and it still prefers your live TradingView chart when it's open.
