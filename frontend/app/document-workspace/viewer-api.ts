/** What an editable viewer gives the workspace: the bytes of the edited copy, built on demand. */
export type ViewerApi = { exportFile(): Promise<Uint8Array> };
