# {{PROJECT_NAME}} frontend

Independent Next.js frontend for the generated `{{PROFILE}}` profile.

```bash
pnpm install
pnpm dev
```

The application runs at http://localhost:3000 and calls the backend directly at the URL in
`NEXT_PUBLIC_API_BASE_URL`. The backend must allow `NEXT_PUBLIC_APP_URL` as a credentialed CORS
origin. Keep `.env` local and commit only `.env.example`.

```bash
pnpm lint
pnpm test
pnpm build
```

Enabled capabilities are recorded in `capabilities.json`. Authentication keeps access tokens only
in memory and restores sessions through the backend refresh cookie. Production frontend and API
origins must use HTTPS. For browser uploads, configure the production S3 provider's bucket CORS to
allow the frontend origin and expose `ETag`.
