-- Single-node counterpart to 023: find every stored copy of a generation_id
-- through the projection's primary index, then read full rows by sort key.
-- The standalone schema set applies this after 006/009/011/013, in the
-- connection's default database, without Keeper or cluster coordination.
--
-- ReplacingMergeTree needs deduplicate_merge_projection_mode = 'rebuild' so
-- deduplicating merges recompute the projection for the merged part.
-- MATERIALIZE builds it for existing parts as a background mutation.
--
-- Leave lightweight_mutation_projection_mode at its default, 'throw': on
-- 26.7.1.1315, 'rebuild' returned deleted rows through the projection until
-- the next merge, and 'drop' lost the touched part's projection even after a
-- forced merge. Use ALTER TABLE ... DELETE, which rewrites the projection
-- with the part; lightweight DELETE remains refused with code 344.
ALTER TABLE activity_generations
    MODIFY SETTING deduplicate_merge_projection_mode = 'rebuild';
ALTER TABLE activity_generations
    ADD PROJECTION IF NOT EXISTS by_generation_id
    (SELECT generation_id, tenant_id, created_at ORDER BY generation_id);
ALTER TABLE activity_generations
    MATERIALIZE PROJECTION by_generation_id;
