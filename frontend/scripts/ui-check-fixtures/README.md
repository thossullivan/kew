Surface modules for tests of the runner itself (`scripts/ui-check-runner.test.ts`),
never for a real run: the test points `npm run ui-check` at one of these folders
with `$KEW_UI_CHECK_DIR`. Each folder is one fixture, holding one module.
