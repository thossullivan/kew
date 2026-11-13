/** A module that ends the process with exit 0 before the runner's summary (#3477). */
export async function checks({ check }) {
  await check('a check before the exit', async () => {});
  process.exit(0);
}
