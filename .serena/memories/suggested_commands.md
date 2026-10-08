Setup: npm i
Dev server: npm run dev
Build: npm run build
Dev build: npm run build:dev
Lint: npm run lint
Preview production build: npm run preview
Backend tests (local venv, NOT docker — runtests.sh hardcodes the NAS path /opt/fund-vista): backend/.venv/bin/python -m pytest backend/tests/ -q (needs fonttools + pytesseract installed in backend/.venv; tesseract binary via brew)
General Darwin utilities: git, ls, cd, grep, find.