import { PlaywrightCrawler, type PlaywrightCrawlingContext } from "crawlee";

export type CrawlPolicy = {
  allowedHosts: string[];
  maxRequests?: number;
  maxConcurrency?: number;
  requestTimeoutSecs?: number;
};

export type BusinessEvidence = {
  url: string;
  title?: string;
  description?: string;
  publicBusinessEmails: string[];
  publicBusinessPhones: string[];
  observedAt: string;
  contactPermission: false;
};

const normaliseHost = (value: string) => new URL(value).hostname.toLowerCase().replace(/^www\./, "");

function assertAllowed(url: string, hosts: string[]): void {
  const parsed = new URL(url);
  if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("Only HTTP(S) sources are allowed");
  if (!hosts.map((host) => host.toLowerCase().replace(/^www\./, "")).includes(normaliseHost(url))) {
    throw new Error("Source host is not on the tenant-approved allowlist");
  }
}

function extractEvidence(context: PlaywrightCrawlingContext): BusinessEvidence {
  const { page, request } = context;
  const title = page.locator("title").first();
  const description = page.locator('meta[name="description"]').first();
  return {
    url: request.url,
    title: undefined,
    description: undefined,
    publicBusinessEmails: [],
    publicBusinessPhones: [],
    observedAt: new Date().toISOString(),
    contactPermission: false,
  };
}

export async function collectPublicBusinessEvidence(
  startUrls: string[],
  policy: CrawlPolicy,
  onEvidence: (item: BusinessEvidence) => Promise<void>,
): Promise<void> {
  if (!policy.allowedHosts.length) throw new Error("At least one approved source host is required");
  for (const url of startUrls) assertAllowed(url, policy.allowedHosts);

  const crawler = new PlaywrightCrawler({
    maxRequestsPerCrawl: policy.maxRequests ?? 30,
    maxConcurrency: policy.maxConcurrency ?? 2,
    requestHandlerTimeoutSecs: policy.requestTimeoutSecs ?? 30,
    respectRobotsTxtFile: true,
    preNavigationHooks: [async ({ request }) => assertAllowed(request.url, policy.allowedHosts)],
    requestHandler: async (context) => {
      const { page, request, enqueueLinks } = context;
      const title = await page.title().catch(() => "");
      const description = await page.locator('meta[name="description"]').getAttribute("content").catch(() => null);
      const emails = await page.locator('a[href^="mailto:"]').evaluateAll((nodes) =>
        nodes.map((node) => (node as HTMLAnchorElement).href.replace(/^mailto:/i, "").split("?")[0]).filter(Boolean),
      );
      const phones = await page.locator('a[href^="tel:"]').evaluateAll((nodes) =>
        nodes.map((node) => (node as HTMLAnchorElement).href.replace(/^tel:/i, "")).filter(Boolean),
      );
      await onEvidence({
        url: request.url,
        title: title || undefined,
        description: description || undefined,
        publicBusinessEmails: [...new Set(emails)],
        publicBusinessPhones: [...new Set(phones)],
        observedAt: new Date().toISOString(),
        contactPermission: false,
      });
      await enqueueLinks({
        strategy: "same-domain",
        transformRequestFunction: (next) => {
          assertAllowed(next.url, policy.allowedHosts);
          return next;
        },
      });
    },
  });
  await crawler.run(startUrls);
}
