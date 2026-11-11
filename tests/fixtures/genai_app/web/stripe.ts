import Stripe from "stripe";

export const stripe = new Stripe(process.env.STRIPE_SECRET_KEY!);

export async function charge(amount: number) {
  return stripe.charges.create({ amount, currency: "usd" });
}
