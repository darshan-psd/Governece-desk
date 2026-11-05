/** What an editable viewer gives the workspace: the bytes of the edited copy, built on demand. */
export type ViewerApi = {
    exportFile(): Promise<Uint8Array>;
    /** The in-progress edits, for the browser-side auto-saved draft (see drafts.ts). */
    draft(): Promise<unknown>;
};
