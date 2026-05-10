# Sports Prediction Bot

A fully-featured Telegram sports prediction bot.

## Features
- Free & Premium predictions with photo + text + optional link
- Premium tips shown as pixelated/watermarked preview until unlocked
- NOWPayments crypto unlock (BTC, ETH, USDT, BNB, SOL, LTC, XRP, DOGE)
- Admin panel: post tips, approve users, broadcast, set WON/LOST/VOID results
- Referral system with configurable commission %
- Tip/donate to admin via crypto
- Full tracking (views, payments) per user and prediction

## User Registration
1. Send `/start` to the bot
2. Join Shuffle Casino via referral link
3. Submit Telegram username for admin approval
4. Admin approves -> user gains access

## Reply Keyboard (Users)
| Button | Action |
|--------|--------|
| Free Tips | View latest free predictions |
| Premium Tips | View premium (locked) predictions |
| My Referral | View referral code and commission earnings |
| Tip Admin | Send crypto donation to admin |
| My Profile | View account info |

## Reply Keyboard (Admin)
| Button | Action |
|--------|--------|
| New Free Tip | Post free prediction |
| New Premium Tip | Post premium prediction with price |
| Approve Users | Review pending registrations |
| Broadcast | Send message to all approved users |
| Stats | View statistics |
| Settings | Set premium price and commission % |

## Environment Variables
```
TELEGRAM_BOT_TOKEN=your_bot_token
NOWPAYMENTS_API_KEY=your_api_key
NOWPAYMENTS_IPN_SECRET=your_ipn_secret
ADMIN_CHAT_IDS=your_telegram_user_id
```

## Deployment
1. Deploy `sports_prediction_bot.py` on CodeWords
2. Set secrets via CodeWords secret manager
3. Visit `https://your-service-url/set-webhook` once after deployment

## Tech Stack
Python 3.11 + FastAPI + Redis + Pillow + httpx + NOWPayments API

## License
MIT
