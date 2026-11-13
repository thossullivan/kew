// The demo build's mark (#3139). The demo shim stamps it on the document, so it
// is in every demo bundle — and `npm run ui-check` fails when it is found
// anywhere under `build/`, which is how a demo module surviving into the
// normal build is caught. Plain JS so the Node-side check imports it as is.
export const DEMO_SENTINEL = 'kew-demo-build-3139';
