// Globals injected by the template / Alpine CDN. Lives in a separate
// `.d.ts` so dashboard.ts can stay a plain script (no `export {}`),
// which lets tsc emit script-style JS for `<script src=…>`.

interface TrustedRouterConfig {
  environment?: string;
  defaultDevUser?: string;
  apiBaseUrl?: string;
  stablecoinCheckoutEnabled?: boolean;
  googleEnabled?: boolean;
  githubEnabled?: boolean;
}

interface EthereumProvider {
  request: (args: { method: string; params?: unknown[] | object }) => Promise<unknown>;
}

interface Window {
  __TR__?: TrustedRouterConfig;
  moneyFromMicrodollars?: (value: unknown) => string;
  ethereum?: EthereumProvider;
}
