declare namespace Cloudflare {
  interface Env {
    DB?: D1Database;
    LOCAL_PREVIEW?: boolean;
    BUCKET?: R2Bucket;
  }
}
