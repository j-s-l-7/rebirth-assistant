# RA web app

Next.js 16 (App Router) client for the RA backend. Pages: onboarding (`/`), portfolio (`/dashboard`), Ask RA (`/chat`), suggestions (`/recommendations`), tax (`/tax`), CAS import (`/import`), risk profile (`/profile`).

```bash
npm install
cp .env.example .env.local   # NEXT_PUBLIC_API_URL defaults to http://localhost:8000/api
npm run dev                  # http://localhost:3000
```

Data is fetched in the browser from the FastAPI backend; the selected investor is kept in localStorage until OTP sign-in ships. Any page accepts `?investor=<id>` to open a specific (demo) investor.
