# MCH PDF Organizer

**Document Merge & PDF Export** for the Madinah Health Cluster tool family. It uses the same
visual identity as the Chart Review Report tool (`Wejdanfb/Chart-Tool`), but it is a separate
application in its own repository.

Upload Excel, Word, PDF and image files, arrange their pages, and download **one** combined PDF.

| Input | How it becomes PDF pages |
|---|---|
| `.pdf` | Kept as-is. Pages are copied, never re-rendered, so quality and selectable text are unchanged. |
| `.png` `.jpg` `.jpeg` | One page per image. JPEG data is embedded unchanged and PNG losslessly. Aspect ratio and phone (EXIF) orientation are respected. You can choose *Fit on A4 page* or *Keep original image size*. |
| `.docx` `.doc` | LibreOffice, headless. Embedded images keep their full resolution. |
| `.xlsx` `.xls` | Each chosen sheet is set to fit one page wide, so no columns are cut off. Wide tables switch to landscape. Use *Choose sheets* to pick which sheets to include. Only print settings change: cell formatting, borders and images are kept. |

**The three steps:**
1. **Upload files.** Each file is converted as it's added. Clear messages explain any file that can't be used.
2. **Organize pages.** Drag pages (touch works too), move a page to a set position, rotate pages, leave pages out, reorder whole documents, or add more files.
3. **Export PDF.** Preview the real combined file, set its name, download it, or start over (asks first).

---

## Deploying from GitHub to Render: step by step

### What gets deployed

```
GitHub repo (this folder)
   |  push to main
   |-- GitHub Actions "Docker build & conversion check"   (.github/workflows/docker-check.yml)
   |     builds the image and tests Word, Excel, PDF, PNG, JPG and JPEG conversion inside it
   |
   '-- Render web service (runtime: Docker, render.yaml)
         Dockerfile: Python 3.12 + LibreOffice 25.2 (Debian 13) + fonts + tini
         gunicorn binds 0.0.0.0:$PORT  (Render sets PORT, default 10000)
         /healthz  -> 200 when LibreOffice works, 503 if not (a failed check blocks the deploy)
```

GitHub Pages can't run this app, because Word and Excel conversion needs LibreOffice on a server.

### Step 1: Create the GitHub repository

1. Sign in to GitHub as **Wejdanfb**.
2. Choose **+ → New repository**:
   - **Name:** `MCH-PDF-Organizer`. Keep it separate from Chart-Tool and Tracer-Summery.
   - **Visibility:** Private or Public. Render works with both.
   - Leave *Add a README*, *.gitignore* and *licence* **unticked**, so the repository starts empty.
3. Upload the project. Use **one** of these:
   - **Web upload:** open the new repository → *uploading an existing file* → drag in **everything inside** the `mch-pdf-organizer` folder. Commit to `main`.
     Hidden items must be included: `.github`, `.gitignore` and `.dockerignore`. On Windows, turn on *View → Show → Hidden items* first. Without `.github` the automatic check won't run.
   - **Git command line:**
     ```
     cd mch-pdf-organizer
     git init -b main
     git add .
     git commit -m "MCH PDF Organizer"
     git remote add origin https://github.com/Wejdanfb/MCH-PDF-Organizer.git
     git push -u origin main
     ```

The repository must contain this layout:

```
MCH-PDF-Organizer/
├── .github/workflows/docker-check.yml
├── .dockerignore   .gitignore
├── Dockerfile      render.yaml     gunicorn.conf.py   requirements.txt
├── app.py   converter.py   pdf_ops.py   storage.py
├── templates/index.html
├── static/  (style.css, app.js, logo.png, vendor/…)
├── tests/   (smoke_api.py, test_server.py, test_e2e.py, make_fixtures.py, fixtures/)
├── run.bat
└── README.md
```

### Step 2: Let the container check pass

Open the repository's **Actions** tab. The run **Docker build & conversion check** starts
automatically and takes about 5–10 minutes. It:

1. builds the exact image Render will use. The build itself converts a Word document and a workbook, and **fails if LibreOffice doesn't work**;
2. checks the fonts inside the image (Arial, Times New Roman, Calibri, Cambria and Arabic substitutes);
3. runs the container the way Render does: `PORT=10000`, 512 MB memory, non-root user;
4. converts **.docx .doc .xlsx .xls .pdf .png .jpg .jpeg**, confirms that password-protected, damaged and unsupported files are refused with clear messages, then builds and downloads a combined PDF;
5. confirms the logs contain no file names or document text;
6. restarts with a fresh container and confirms old files are gone.

A green tick means the Docker configuration and conversion dependencies work. If it's red,
open the failed step: the log shows exactly what broke. Don't deploy until it's green.

### Step 3: Create the Render service

**Option A: Blueprint** (uses `render.yaml`; recommended)
1. In <https://dashboard.render.com>, choose **New → Blueprint**.
2. Connect GitHub if asked, and give Render access to `MCH-PDF-Organizer`.
3. Select the repository and branch `main`. Render shows the service `mch-pdf-organizer` with these settings:
   - Docker runtime, **Frankfurt** region, **0.5c-512mb** instance, health check `/healthz`.
   - Auto-deploy only when the GitHub checks pass.
4. Click **Apply**.

**Option B: manual Web Service**
1. **New → Web Service** → select the repository → branch `main`.
2. **Language / Runtime:** **Docker**. Leave *Dockerfile path* as `./Dockerfile` and the *Docker command* empty.
3. **Region:** Frankfurt. **Instance type:** 0.5 CPU / 512 MB or larger.
4. **Advanced → Health Check Path:** `/healthz`.
5. **Advanced → Auto-Deploy:** *After CI checks pass*.
6. **Create Web Service**.

You don't need to set `PORT` or any start command. Render provides `PORT`, and the image starts
gunicorn on `0.0.0.0:$PORT` by itself.

### Step 4: Watch the first deploy

The first build installs LibreOffice and fonts, which takes several minutes. In the service's
**Events / Logs** you should see:

- in the build log: `Fonts for common Office faces:` with `Arial -> Liberation Sans` and `Calibri -> Carlito`, then the self-test line `{"office_version": "LibreOffice 25.2…", "word": true, "excel": true, … "ok": true}`;
- in the runtime log: `Listening at: http://0.0.0.0:10000` and `office self-test passed (LibreOffice 25.2…)`.

The service becomes **Live** only after `/healthz` answers 200.

### Step 5: Check the live service

1. Open `https://<your-service>.onrender.com/healthz`. You should see `"ok": true`, `"office_converter": true` and `"office_selftest": "ok"`.
2. Open `https://<your-service>.onrender.com/` and try a few real files, on a computer **and on an iPhone in Safari**.
3. Optional full check from any computer with Python 3:
   ```
   python tests/smoke_api.py https://<your-service>.onrender.com tests/fixtures
   ```
   It ends with **ALL CHECKS PASSED**.

### Step 6: Updates

Commit to `main`. The GitHub check runs, and when it passes Render rebuilds and redeploys
automatically. Every redeploy starts with an empty disk, so anyone with files open at that
moment is told to start over.

### If something goes wrong

| Symptom | Cause / fix |
|---|---|
| Build fails at `apt-get install … libreoffice-writer …` | Debian package servers had a temporary problem. Use *Manual Deploy → Clear build cache & deploy*. |
| Build log shows `WARNING: font package … not available` | That font package was renamed in Debian. The app still works with substitute fonts. Report the package name so it can be updated. |
| Build fails at `python converter.py --self-test` | LibreOffice can't convert inside the image. The printed JSON shows `error`. Nothing is deployed, and the previous version keeps running. |
| Deploy fails: health check | `/healthz` returned 503 because LibreOffice is missing or failed at start-up. The runtime log shows `office self-test FAILED: …`. |
| "Out of memory" / instance restarts during big Excel files | Increase the instance type to **1c-2g**. |
| Users see "Your session on the server has expired" | The service restarted or redeployed (the disk is wiped), or files sat unused longer than `SESSION_TTL_MINUTES`. They add the files again. |

---

## Privacy and data handling

- **Where files go:** files are uploaded to and processed **only on this service**, in a temporary per-session folder. No external conversion service is used, and the page loads no outside scripts or fonts.
- **Region:** on Render, processing happens in the chosen region, **Frankfurt (Germany)** in `render.yaml`. Render has no Middle East region. Check this against the hospital's data-handling policy before using it for patient-identifiable documents. For documents that must not leave the hospital network, run it on an internal machine (Docker or `run.bat`).
- **Nothing is stored permanently.** Session folders (originals, converted pages, combined PDF) are deleted:
  - immediately on **Start over**;
  - automatically after `SESSION_TTL_MINUTES` (default 120) without use;
  - and on every restart or redeploy, because Render's filesystem is ephemeral.

  Converted files are written atomically, so a crash never leaves half-written PDFs.
- **Logs** contain only the file type, page count, timing and error codes. They never contain file names or document text. Gunicorn's access log is off. Both the GitHub check and `tests/test_server.py` verify this.

## Limits and timeouts (environment variables)

| Name | Default | Meaning |
|---|---|---|
| `MAX_UPLOAD_MB` | 50 | Largest single file (413 with a clear message above this) |
| `MAX_SESSION_MB` | 300 | All files of one user together |
| `MAX_PAGES` | 3000 | Pages in one combined PDF |
| `MIN_FREE_DISK_MB` | 200 | Uploads are refused (507, "try again later") below this much free disk |
| `SOFFICE_TIMEOUT` | 240 | Seconds before a stuck Word or Excel conversion is stopped. LibreOffice is killed, and tini reaps the process. |
| `SESSION_TTL_MINUTES` | 120 | Delete unused sessions after this long |
| `GUNICORN_TIMEOUT` | 300 | Worker timeout (kept above `SOFFICE_TIMEOUT`) |
| `GUNICORN_THREADS` | 8 | Concurrent requests: thumbnails, previews, downloads |
| `WEB_CONCURRENCY` | 1 | Gunicorn processes. Keep at 1 on 512 MB, because LibreOffice runs one conversion at a time. |

## Run locally

- **Windows, for testing or internal use:** install Python 3.10+ (tick *Add python.exe to PATH*) and [LibreOffice](https://www.libreoffice.org/download/), then double-click `run.bat`.
  - It opens `http://127.0.0.1:5052` and is only reachable from that PC.
  - Port 5052 doesn't clash with the Chart Review (5050) or Tracer Summary (5051) tools.
- **Docker, exactly as on Render:**
  ```
  docker build -t mch-pdf-organizer .
  docker run --rm -p 10000:10000 -e PORT=10000 mch-pdf-organizer      # http://localhost:10000
  ```
- **Plain Python on Linux or macOS:** `pip install -r requirements.txt && python app.py` binds `HOST` (default `0.0.0.0`) and `PORT` (default 5052).

## Tests

| Command | What it checks |
|---|---|
| `python tests/smoke_api.py <url> tests/fixtures` | Every format through the API, error cases, sheet choice, order and rotation, download, cleanup. Uses only the standard library, so it also works against the live URL. |
| `python tests/test_server.py tests/fixtures` | Timeout kill, export-option fallback, disk and session limits, page limit, path safety, `/healthz` 503, profile cleanup, sweeper, log privacy |
| `python tests/make_fixtures.py fixtures`, then `BASE=<url> FX=fixtures python tests/test_e2e.py` | Real-browser acceptance tests (16 scenarios). Every downloaded PDF is checked page by page. |

## Licences

Bundled: pdf.js 3.11 (Apache-2.0) and SortableJS 1.15 (MIT). Their licence files are in `static/vendor/`.

© 2026 Wejdan Boharam. All Rights Reserved.
