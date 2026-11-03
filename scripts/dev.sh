#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Local development stack bootstrap. Placeholder: the real script (database,
# seeded synthetic data, API with reload, Vite dev server) lands in Phase 7.
set -euo pipefail

echo "scripts/dev.sh: the local dev stack is not available until Phase 7." >&2
echo "Meanwhile: 'cd backend && uv run perfin api' and 'cd frontend && npm run dev'." >&2
exit 2
