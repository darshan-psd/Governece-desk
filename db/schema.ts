import { sqliteTable, text, integer, primaryKey, index } from 'drizzle-orm/sqlite-core';
export const records = sqliteTable('records', {
  owner: text('owner').notNull(), id: text('id').notNull(), kind: text('kind').notNull(),
  data: text('data').notNull(), revision: integer('revision').notNull().default(1), updated: text('updated').notNull(),
}, t => [primaryKey({columns:[t.owner,t.id]}),index('records_owner_kind').on(t.owner,t.kind)]);
export const audit = sqliteTable('audit', {
  id: text('id').primaryKey(), owner: text('owner').notNull(), action: text('action').notNull(), subject: text('subject').notNull(), at:text('at').notNull()
}, t=>[index('audit_owner_at').on(t.owner,t.at)]);
