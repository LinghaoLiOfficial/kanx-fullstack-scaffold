# {{PROJECT_NAME}}

Generated full-stack project using the `{{PROFILE}}` profile.

## Run everything

Install the frontend dependencies once, then start both applications:

```bash
cd frontend && pnpm install && cd ..
make dev
```

- Frontend: http://localhost:3000
- Backend API: http://localhost:8000
- API documentation: http://localhost:8000/docs

`make dev` only coordinates the two applications. Backend infrastructure remains owned by
`backend/compose.yml` and stays running when the development processes stop.

## Run independently

```bash
cd frontend
pnpm install
pnpm dev
```

```bash
cd backend
make dev
```

Both directories contain their own environment files, documentation, dependencies and build
commands. They may be moved into separate repositories or deployed independently. The generator
does not initialize Git.

The selected capability composition is fixed after generation. Add product code normally rather
than re-running a module installer against this project.
