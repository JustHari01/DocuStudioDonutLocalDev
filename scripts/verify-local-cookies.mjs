import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { CdpClient } from "../e2e/lib/cdp.mjs";

const audit = process.argv[2];
assert.ok(audit, "Pass the local integration audit directory");
const expected = JSON.parse(
  await readFile(`${audit}/cookies-integrity.json`, "utf8"),
);
const cdp = await CdpClient.connect(19471);
let cookies = 0;
try {
  for (const database of expected.databases) {
    const profileId = database.path.split("/")[0];
    // Only aggregate counts/hashes return through CDP; cookie values stay in the page.
    const result = await cdp.evaluate(`(async()=>{
      const result=await window.__TAURI_INTERNALS__.invoke("read_profile_cookies",{profileId:${JSON.stringify(profileId)}});
      const values=result.domains.flatMap(group=>group.cookies);
      const hash=async(text)=>Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(text)))).map(b=>b.toString(16).padStart(2,"0")).join("");
      const hashes=await Promise.all(values.map(cookie=>hash(JSON.stringify([cookie.domain,cookie.path,cookie.name,cookie.value]))));
      return {count:values.length,digest:await hash(hashes.sort().join(String.fromCharCode(10)))};
    })()`);
    assert.equal(result.count, database.cookies, profileId);
    assert.equal(
      result.digest,
      database.cookie_value_digest,
      "Every cookie value must match independent AES-GCM decryption",
    );
    cookies += result.count;
  }
  const result = {
    profiles: expected.databases.length,
    cookies,
    nativeWindowsReader: "passed",
    valuesLogged: false,
  };
  await writeFile(
    `${audit}/native-cookie-reader.json`,
    JSON.stringify(result, null, 2),
  );
  console.log(JSON.stringify(result));
} finally {
  cdp.close();
}
