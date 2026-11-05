// Auto-saved drafts of in-progress document edits, kept in this browser only (IndexedDB).
//
// A draft is keyed by the person and the exact document version being edited, and remembers that
// version's SHA-256, so it is only ever restored onto the file it was made from. Nothing here is
// sent to the server: a real version is still created only by "Save as new version". Drafts older
// than 30 days are removed. Every call fails quietly (e.g. private browsing): drafts are a safety
// net, never a requirement.
export type Draft = { base: string; kind: string; savedAt: string; data: unknown };

const DB = 'gov-review-drafts', STORE = 'drafts', MAX_AGE_MS = 30 * 24 * 3600 * 1000;

function open(): Promise<IDBDatabase> {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open(DB, 1);
        request.onupgradeneeded = () => request.result.createObjectStore(STORE);
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
    });
}

async function run<T>(mode: IDBTransactionMode, work: (store: IDBObjectStore) => IDBRequest<T> | void): Promise<T | undefined> {
    try {
        const db = await open();
        return await new Promise<T | undefined>((resolve, reject) => {
            const tx = db.transaction(STORE, mode), request = work(tx.objectStore(STORE));
            tx.oncomplete = () => { db.close(); resolve(request ? request.result : undefined) };
            tx.onerror = tx.onabort = () => { db.close(); reject(tx.error) };
        });
    } catch { return undefined }
}

export const draftKey = (person: string, versionId: string) => `${person}:${versionId}`;
export const getDraft = (key: string) => run<Draft>('readonly', s => s.get(key));
export const putDraft = (key: string, draft: Draft) => run('readwrite', s => s.put(draft, key));
export const deleteDraft = (key: string) => run('readwrite', s => s.delete(key));

export function pruneDrafts() {
    return run('readwrite', store => {
        const cursor = store.openCursor();
        cursor.onsuccess = () => {
            const c = cursor.result; if (!c) return;
            const at = Date.parse((c.value as Draft)?.savedAt || '');
            if (!(at > Date.now() - MAX_AGE_MS)) c.delete();
            c.continue();
        };
    });
}
