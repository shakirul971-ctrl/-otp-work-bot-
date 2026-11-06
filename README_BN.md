# Telegram Work Bot (নিরাপদ টাস্ক সাবমিশন + BEP20 হিসাব)

## গুরুত্বপূর্ণ
এই প্রজেক্টটি সাধারণ, অনুমোদিত টাস্কের প্রমাণ জমা ও Admin review-এর জন্য। কোনো Instagram/অন্যান্য অ্যাকাউন্টের OTP, পাসওয়ার্ড, 2FA বা verification code সংগ্রহ/বিক্রি করার ফিচার নেই। পেমেন্ট স্বয়ংক্রিয়ভাবে ব্লকচেইনে পাঠায় না; Admin নিজে ওয়ালেট/নেটওয়ার্ক যাচাই করে পেমেন্ট পাঠিয়ে TXID নথিভুক্ত করবেন।

## ১) Telegram তথ্য
1. `@BotFather` থেকে bot token নিন।
2. নিজের Telegram numeric user ID জানুন (বিশ্বস্ত ID-bot বা নিজের bot-এর update/log থেকে)।
3. Token কাউকে দেবেন না। `.env` ফাইলে token রাখুন, GitHub-এ আপলোড করবেন না।

## ২) Online server (Docker VPS)
Ubuntu VPS-এ Docker/Compose ইনস্টল করে এই ফোল্ডার আপলোড করুন। তারপর:
```bash
cp .env.example .env
nano .env
```
`BOT_TOKEN`-এ BotFather token এবং `ADMIN_IDS`-এ নিজের numeric Telegram ID দিন।
তারপর `docker compose up -d --build` চালান। লগ দেখতে `docker compose logs -f`।

`docker-compose.yml`:
```yaml
services:
  bot:
    build: .
    env_file: .env
    restart: unless-stopped
    volumes:
      - botdata:/data
volumes:
  botdata:
```

## ৩) Admin কমান্ড
- `/addtask শিরোনাম | বিবরণ` — টাস্ক তৈরি
- `/pending` — pending submission ও withdrawal
- `/approve_task SUBMISSION_ID AMOUNT` — টাস্ক অনুমোদন করে ব্যালেন্সে USDT যোগ
- `/reject_task SUBMISSION_ID কারণ` — সাবমিশন বাতিল
- `/paid WITHDRAWAL_ID TXID` — বাস্তবে পেমেন্ট পাঠানোর পর paid চিহ্নিত
- `/refund WITHDRAWAL_ID কারণ` — withdrawal বাতিল করে ব্যালেন্স ফেরত

## ৪) User কমান্ড
- `/start`, `/help`, `/tasks`, `/submit TASK_ID প্রমাণ`
- `/profile`, `/balance`, `/history`, `/referral`
- `/withdraw AMOUNT BEP20_WALLET_ADDRESS`

## ৫) নিরাপত্তা ও আর্থিক সতর্কতা
- BEP20 ঠিকানা সাধারণত `0x` দিয়ে শুরু, 42 অক্ষর; শুধু ফরম্যাট যাচাই নিরাপদ ঠিকানা নিশ্চিত করে না।
- USDT-এর সঠিক contract/network যাচাই করুন; ভুল নেটওয়ার্কে পাঠালে অর্থ হারাতে পারেন।
- BNB Smart Chain-এ ট্রান্সফারের gas fee-এর জন্য BNB লাগতে পারে।
- প্রথমে অল্প পরিমাণে test transfer করুন। কখনো seed phrase/private key বট বা সার্ভারে রাখবেন না।
- Admin account-এ 2FA চালু রাখুন, VPS firewall/updates বজায় রাখুন, DB backup নিন।
- Production ব্যবহারের আগে duplicate request, audit log, rate limiting, privacy notice ও স্থানীয় আইন/পেমেন্ট নীতিমালা যাচাই করুন।
