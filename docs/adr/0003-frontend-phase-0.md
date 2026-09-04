# ADR 0003: No-build control page for Phase 0

- Status: accepted for Phase 0; superseded when the full Control Center begins
- Decision: FastAPI serves a small static control page for health, pause/resume, and the synthetic Decision Inspector.
- Rationale: Node is absent on the current machine and Phase 0 needs to validate the safety boundary, not a frontend toolchain. React/Vite remains the preferred later UI once product pages are implemented.

