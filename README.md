# Yoga-Code AI (login + health profile edition)

Real-time AI yoga posture coach. Pose tracking runs in your browser (MediaPipe); the backend saves accounts,
health answers, practice history and uploaded photos. **Camera video is never sent to the server.**

## How the app flows
1. Opening the site always shows the **login page** (`login.html`). New users click **Sign up** and give a
   **username**, email and password. Log in works with email *or* username.
2. First login only: the **health questions** page (`onboarding.html`) asks age, gender, height, weight and
   whether you have any past medical history (conditions, details, medications).
3. After that you go straight to the yoga coach (`index.html`). Next time you log in, everything
   (profile, health answers, sessions, photos) is remembered, and you skip the questions.
   You stay logged in for 30 days on the same browser.
4. Edit your answers any time: profile menu (top right) -> **Health profile**.

## Run it
Requires Python 3.9+ only (standard library, no pip install).

    cd backend
    python server.py          # Mac/Linux: python3 server.py

Open **http://localhost:8000** (not the HTML files directly, and not Live Server: page and API must share one address).
Internet is needed for the CDN libraries (Bootstrap, MediaPipe, fonts).

## Folders
    backend/server.py        web server + API + database
    backend/poses.json       pose library
    backend/media/poses/     pose illustrations (SVG)
    backend/data/            created on first run: yoga.db (SQLite) + uploads/   <- BACK THIS UP
    frontend/login.html      login / sign up (first page)
    frontend/onboarding.html health questions (first login) and health profile editor
    frontend/index.html      the yoga coach (redirects to login if you are not logged in)
    frontend/js/             common.js (login/onboarding helpers), extras.js (coach <-> API)
    frontend/css/            auth.css (login/onboarding), extras.css

If you used the previous version, an existing database upgrades automatically (existing users get a
username equal to their name... and will see the health questions on next login).

## What gets saved
- Account: username, email, password (salted PBKDF2 hash, never plain text), goal, level, optional photo
- Health profile: age, gender, height, weight, medical history yes/no, conditions, details, medications
- Each practice: pose, duration, best hold, holds completed, date
- Progress photos (max 60 per user, 5 MB each, resized in the browser)
- Users can export all their data (includes health profile) or delete their account (removes everything)

## API (under /api, JSON, cookie login)
    POST /auth/register {username,email,password} | /auth/login {email (or username),password} | /auth/logout
    GET  /me      PATCH /me
    GET  /me/health   PUT /me/health {age,gender,height_cm,weight_kg,has_history,conditions[],details,medications}
    POST /me/avatar   GET /me/avatar   GET /me/export   POST /me/delete
    GET  /sessions   POST /sessions   DELETE /sessions/<id>
    GET  /stats?today=YYYY-MM-DD
    GET  /photos   POST /photos?pose=&caption=   DELETE /photos/<id>   GET /photos/<id>/file
Mutating requests need the header `X-Requested-With: yoga`.

## Security built in
Password hashing, HttpOnly SameSite cookies, CSRF header + origin check, login/register rate limiting,
uploads verified by file signature, owner-only file access, parameterised SQL, escaped output.
Health data is sensitive: keep `backend/data/` private and back it up.

## Putting it online
1. Use a host with a **persistent disk** (set `YOGA_DATA=/path/to/disk`).
2. Serve over HTTPS and start with `YOGA_SECURE=1 HOST=0.0.0.0 PORT=8000 python server.py`.
3. Add a privacy policy page since you store emails, health answers and photos.
