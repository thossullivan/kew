/**
 * A route handler that throws while its check awaits the request in the page
 * (#3477): the runner aborts the route, so the fetch fails at once instead of
 * waiting forever, the check fails with the handler's error, and the check
 * after it still runs.
 */
export async function checks({ check, visit, expect }) {
  await check('a route handler that throws fails its check', () =>
    visit('/work', async (page) => {
      await page.route('**/api/ui-check-thrown', () => {
        throw new Error('the handler threw on purpose');
      });
      const answer = await page.evaluate(() =>
        fetch('/api/ui-check-thrown').then(
          () => 'answered',
          () => 'aborted'
        )
      );
      expect(answer === 'aborted', `the fetch was ${answer}`);
    })
  );
  await check('the check after it still runs', async () => {});
}
