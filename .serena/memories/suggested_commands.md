Setup: npm i
Dev server: npm run dev
Build: npm run build
Dev build: npm run build:dev
Lint: npm run lint
Preview production build: npm run preview
Backend tests (local venv, NOT docker): backend/.venv/bin/python -m pytest backend/tests/ -q (needs fonttools + pytesseract installed in backend/.venv; tesseract binary via brew). Deploy to the Mac: sh ~/srv/fund-vista/ops/deploy-mac.sh [sha] (the push to main deploys by itself; logs in ~/srv/ci/cd/logs).
General Darwin utilities: git, ls, cd, grep, find.