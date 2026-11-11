// A page that talks to services straight from the browser.
export default function Page() {
  const password = process.env.NEXT_PUBLIC_DB_PASSWORD;
  const stripeKey = process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY ?? "";
  // process.env.NEXT_PUBLIC_COMMENTED_OUT
  const load = async () => fetch("https://api.github.com/repos/acme/app/issues");
  return <div data-key={stripeKey} data-pw={password} onClick={load} />;
}
