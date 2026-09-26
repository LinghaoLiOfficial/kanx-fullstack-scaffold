# {{PROJECT_NAME}}

Generated full-stack project using the `{{PROFILE}}` profile.

## Run everything

Install the frontend dependencies once, then start both applications from this project root:

```bash
cd your-frontend-directory && pnpm install && cd ..
make dev
```

- Frontend: http://localhost:3000
- Backend API: http://localhost:8000
- API documentation: http://localhost:8000/docs

`make dev` discovers the backend by its `module.toml` and the frontend by its `package.json`, so
you may rename or move those two directories. Backend infrastructure remains owned by the
backend's `compose.yml` and stays running when the development processes stop.

## Run independently

```bash
cd your-frontend-directory
pnpm install
pnpm dev
```

```bash
cd your-backend-directory
make dev
```

Both directories contain their own environment files, documentation, dependencies and build
commands. They may be moved into separate repositories or deployed independently. The generator
does not initialize Git.

The selected capability composition is fixed after generation. Add product code normally rather
than re-running a module installer against this project.
