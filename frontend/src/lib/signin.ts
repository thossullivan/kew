// Shared sign-in teaching copy (CS-3). Any auth-gated page renders the
// recovery instructions from this single source so the wording can't drift.
// `kew serve` prints a sign-in link at startup; `kew serve --new-token`
// rotates it — a signed-out operator needs exactly these two commands.

export type SignInCommand = {
  cmd: string;
  desc: string;
};

/** The two commands a signed-out operator needs, in order. */
export const SIGNIN_COMMANDS: readonly SignInCommand[] = [
  { cmd: 'kew serve', desc: 'prints a sign-in link in your terminal at startup' },
  { cmd: 'kew serve --new-token', desc: 'rotates the link if you lost it' }
];

export const SIGNIN_HEADING = 'Not signed in';

export const SIGNIN_BODY =
  'Approving holds and controlling the fleet need a session. The link is printed ' +
  'in the terminal running kew serve — open it here to sign in.';
