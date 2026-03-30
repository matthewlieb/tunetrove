export type ParsedBody = {
  json: boolean;
  data: unknown;
  raw: string;
};

export async function readResponseBody(res: Response): Promise<ParsedBody> {
  const raw = await res.text();
  try {
    const data = JSON.parse(raw) as unknown;
    return { json: true, data, raw };
  } catch {
    return { json: false, data: null, raw };
  }
}
