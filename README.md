# Copper scanner

A phone page and Telegram alerts for Kalshi's weekly copper contracts (KXCOPPERW). Every hour, GitHub runs a scan that scores each strike with your rule and posts the results to the page. On weekends, it messages you when there's a bet worth taking.

**Page:** https://dusterjr.github.io/copper-scanner/

It only reads public data. It never places bets: you place every bet yourself in the Kalshi app.

## Each weekend

The betting window runs from Friday 4 PM to Sunday 5 PM Chicago time (Friday 5 PM to Sunday 6 PM New York), while COMEX copper is closed. That's where the backtest found the edge.

1. Open the page, or wait for the Friday Telegram message.
2. Check that **Copper price used** matches the copper price Kalshi shows. If it doesn't, type Kalshi's price into the box. The page recalculates instantly.
3. Bet every strike listed under **bets to take**, 1 contract each, and nothing else.
4. Don't pay more than **Pay at most** for any bet. Above that, the bet no longer clears 4¢.
5. Tap **Placed all** in Telegram (or send `placed 6.54 6.60` for some of them) so the live test records what you actually bet.

**Paper only** sections (plain count picks, weekday picks) are tracked for the live test. Don't bet them.

**When to stop:** after 6 betting weekends, compare the live results with the backtest. Stop sooner if you're ever down about $15.

## Telegram

You'll get messages only during the weekend window, unless you switch weekday alerts on:

- **Friday, when the window opens:** the bets to take, or "No bets this weekend."
- **When the list changes:** new bets that qualify, and bets that no longer do.
- **If a scan fails:** one message, then another when it's working again.

Replies the scanner understands:

| Send | What happens |
|---|---|
| `Seen` | No more alerts about the bets it just sent |
| `Placed all` | Same, and records that you placed them |
| `placed 6.54 6.60` | Records specific strikes as placed |
| `/price 6.58` | Uses Kalshi's copper price for this weekend's scans (`/price clear` to undo) |
| `/weekday on` / `/weekday off` | Weekday alerts (off by default) |
| `/status` | The current bets |
| `/help` | This list |

The scanner reads messages once an hour, at the start of each scan and before any alert goes out. Your reply always counts, but its confirmation can take up to an hour.

## One-time setup

1. **Turn on the page.** In this repo: Settings → Pages → Source: "Deploy from a branch" → branch `main`, folder `/ (root)` → Save. The page goes live within a few minutes.
2. **Create your Telegram bot.**
   - In Telegram, message **@BotFather** and send `/newbot`. Pick a name (for example, "Isaac copper alerts") and a username ending in `bot`. BotFather replies with a **token**, a long code. Keep it private, and never paste it into a chat.
   - Open your new bot, tap **Start** and send it `hi`. Bots can't message you first.
3. **Give GitHub the token.** In this repo: Settings → Secrets and variables → Actions → New repository secret. Name: `TELEGRAM_BOT_TOKEN`. Secret: the token.
4. **Get your chat ID from the bot.** Actions tab → Copper scan → Run workflow (or wait for the next hourly scan). Your bot replies "Your chat ID is …". Add that number as a second secret named `TELEGRAM_CHAT_ID`.
5. **Test it.** Run the workflow again. Within a couple of minutes you should get "Setup complete" in Telegram, plus replies to anything you sent the bot earlier.
6. **Put it on your phone.** Open the page in Safari → Share → Add to Home Screen. On Android: Chrome menu → Add to Home screen.

## Running a scan yourself

- **Refresh** on the page: during the weekend window, it pulls Kalshi's latest prices straight from your phone and recalculates. If Kalshi can't be reached from the page, it links you to GitHub instead.
- **On GitHub:** Actions tab → Copper scan → Run workflow. Fresh results in about 2 minutes.
- The original Colab notebooks still work as a backup.

## Settings (optional)

In this repo: Settings → Secrets and variables → Actions → **Variables** tab → New repository variable.

| Variable | Example | What it does |
|---|---|---|
| `WEEKDAY_ALERTS` | `true` | Forces weekday alerts on (or use `/weekday on` in Telegram) |
| `WEEKDAY_THRESHOLD` | `0.06` | A stricter bar for weekday picks (default 0.04) |
| `DISPLAY_TZ` | `Asia/Jerusalem` | Time zone for times in Telegram messages (default `America/Chicago`). The page always uses your phone's time zone. |

## How it decides

- **Starting price:** COMEX copper's latest price (Yahoo `HG=F`), or your `/price` during the weekend.
- **Chance of finishing above each strike:** every past week of copper moves since 2018, each scaled to how jumpy copper was at the time, then rescaled to today's 20-day average daily move, with the 2019–2026 uptrend removed.
- **Edge** = your chance − price − Kalshi's fee (0.07 × price × (1 − price)). A bet qualifies at an edge of 4¢ or more.
- **Pay at most** is the highest price that still clears 4¢ after the fee.

Backtest (Apr–Oct 2026, 1 contract per bet): weekend bets averaged about +24¢ per bet across 17 weekends, with 3 small losing weekends. Weekday bets showed no reliable edge, so weekday picks are paper only. 2026 was a rising copper market, so the live test decides whether this holds.

## Records

| File | What's in it |
|---|---|
| `data/latest.json` | The latest scan, which the page shows |
| `data/log/runs.csv` | One line per scan |
| `data/log/<week>.csv` | Every flagged pick, with prices, for each week |
| `data/log/acks.csv` | What you marked Seen or Placed in Telegram |
| `data/state.json` | Alert memory: what's been sent, acknowledged and switched on |
| `data/copper_history.csv` | The scanner's own copy of daily copper prices since 2018 |

## When something goes wrong

- **The page says the last scan failed:** usually Kalshi or Yahoo didn't respond, and the next hourly scan normally recovers. If Yahoo blocks the download, the scanner falls back to its stored price history and says so.
- **No Friday message:** check the Actions tab for a failed run, and check that both Telegram secrets are set.
- **GitHub disabled the schedule:** GitHub can pause scheduled runs in repos with no activity for 60 days. The hourly saves should prevent that, but if you get the email, open the Actions tab and click Enable.

## Limits

- Prices on the page can be up to an hour old unless Refresh reaches Kalshi. Always check Pay at most.
- GitHub's hourly runs can start 10–30 minutes late, so alerts aren't instant.
- This repo is public, so anyone with the link can see the page and the rule.
