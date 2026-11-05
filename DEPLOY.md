# Deploying

The app is a single Docker container: the API and the demo page on one port, with no database
and no disk writes. Any host that runs a Dockerfile works. These steps use **Render**, which
has a free tier and reads the included [`render.yaml`](render.yaml).

The container listens on `$PORT` (default 8000), exposes `/health` for health checks, and runs
as a non-root user. It uses about 90 MB of memory, so the 512 MB free instance is enough.

## Render (free tier)

1. **Push the repository to GitHub** (already done if you are reading this there).
2. **Create the service.** In the Render dashboard, choose **New → Blueprint**, connect your
   GitHub account and pick this repository. Render reads `render.yaml` and proposes one web
   service, `energy-bill-extractor`, built from the `Dockerfile` on the free plan.
3. **Fill in the secret.** Render asks for `ANTHROPIC_API_KEY`, because it is marked
   `sync: false`. Leave it empty to deploy the demo mode first (no model, no cost), or paste
   a key from console.anthropic.com.
4. **Apply.** The first build takes about 3–5 minutes. When the status shows *Live*, open
   `https://energy-bill-extractor-XXXX.onrender.com`. The demo page loads and the four
   sample bills work.
5. **Switch on the real model** (optional). Go to **Environment**, set
   `BILLX_BACKEND=anthropic`, check that `ANTHROPIC_API_KEY` is set, and save. Render
   redeploys. `/health` should now report `"backend": "anthropic"`.
   `BILLX_ANTHROPIC_MODEL` is set to `claude-haiku-5-5` in the blueprint (about $0.0008 per
   bill). Change it to `claude-sonnet-5-5` for the highest accuracy (about $0.013 per bill).
6. **Check it.**

   ```bash
   curl https://YOUR-SERVICE.onrender.com/health
   ```

   ```bash
   curl -F "file=@bill_extractor/static/samples/bill_000.pdf" https://YOUR-SERVICE.onrender.com/v1/bills/extract
   ```

### Before sharing a real-model deployment publicly

- **Anyone with the URL can spend your API credits.** The API has no authentication or rate
  limiting. Keep `BILLX_BACKEND=mock` for a public portfolio demo, or put the service behind
  an auth proxy (Cloudflare Access, or Render's private services plus a gateway).
- **Set a spend limit** on the Anthropic key in the Console.
- **Cold starts.** Free instances sleep after about 15 minutes idle; the first request then
  takes 30–60 seconds. Use a paid instance to avoid this.
- **CORS** stays off (same-origin only) unless you set `BILLX_CORS_ORIGINS` to the origins of
  another front end.
- **Uploads** are capped at 10 MB and 40 megapixels; adjust `BILLX_MAX_UPLOAD_MB` and
  `BILLX_MAX_IMAGE_MEGAPIXELS` if needed.

## Other hosts

The same image runs anywhere:

- **Fly.io.** Run `fly launch` (it detects the Dockerfile; answer *no* to databases), then
  `fly secrets set ANTHROPIC_API_KEY=...` and `fly deploy`. Set the internal port to 8000
  when asked.
- **Google Cloud Run.** Run
  `gcloud run deploy energy-bill-extractor --source . --set-env-vars BILLX_BACKEND=mock`.
  Cloud Run sets `$PORT` itself.
- **Any VM.** Run `docker compose up -d --build` with a `.env` file next to it.
