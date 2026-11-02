export function SignIn() {
  return (
    <div className="signin">
      <p>Sign in with GitHub to see the runs of repositories you can read.</p>
      <a className="button" href="/auth/login">
        Sign in with GitHub
      </a>
    </div>
  );
}
