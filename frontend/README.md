# RA web app

Next.js 16 (App Router) chat client for the RA backend: one page, `/chat` (`/` redirects there).

`app/layout.tsx` is the document shell (light theme); `app/(chat)/` holds the standalone white chat window (`components/chat/`). Each browser chats as its own guest: the guest investor id is kept in localStorage until sign-in ships, and the latest conversation reloads on refresh.

```bash
npm install
cp .env.example .env.local   # NEXT_PUBLIC_API_URL defaults to http://localhost:8000/api
npm run dev                  # http://localhost:3000/chat
```

Answers stream from `POST /api/chat/sessions/{id}/messages` as server-sent events (`status`, `text`, `done`, `error`); `done.text` is the compliance-checked answer and replaces the streamed text. See `lib/api.ts` and `components/chat/useChat.ts`.
