- Status: todo
- Created: 2026-11-24
- Last updated: 2026-11-24
- Owner: Péter Kiss (peter.kiss@pepperstone.com)

# B-003 — Production STL toolchain runtime install (T7 follow-up)

`POST /v1/stl/import` and `POST /v1/stl/views` are implemented, but `slice3d`
and `trimesh[easy]` MUST NOT be added to `backend/uv.lock`: the shop licence
inventory is coupled to that lock file (`docs/CR-008-shop-licence-inventory-coupling.md`).
The runtime toolchain is therefore an explicit install step on the production
backend host, not a Python dependency of the backend image. Deployment is
deferred from this backend change, so this ticket names the owner in the same
change as required by `AGENTS.md` ("One sheet source", deferred follow-ups).

## Definition of Done

- [ ] Owner installs `slice3d` and `trimesh[easy]` into the Python runtime that
      serves `backend.penplot` on the Mac production host.
- [ ] Owner runs `PENPLOT_STL_REQUIRE_TOOLCHAIN=1 python3 -m pytest
      backend/tests/test_penplot_stl_views.py -q` on that host and it is green.
- [ ] Owner smoke-tests `POST /v1/stl/import` and `POST /v1/stl/views` with a
      sample STL and confirms a `broad, edge, end` response with server-side
      stats.
- [ ] Owner confirms that toolchain absence answers the documented 503
      `stl_toolchain_unavailable` envelope (never a raw traceback).
- [ ] `backend/uv.lock` and the shop licence-inventory digest stay unchanged.
- [ ] Sample STL rights (Knipex XS, crescent wrench pair) remain blocked before
      sale while shop ticket S15 is open; this ticket MUST NOT enable sample
      sales.

## Notes

- Backend code and tests are landed in this change; only the production
  runtime install is outstanding.
- `python3 -m pytest backend/tests/test_penplot_stl_views.py -q` runs the full
  geometry suite on hosts with the toolchain. `backend/.venv` skips those cases
  by design and keeps one `test_toolchain_importable` opt-in check.
