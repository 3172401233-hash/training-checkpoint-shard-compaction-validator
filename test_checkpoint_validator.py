import unittest

from checkpoint_validator import (
    CompactionValidator,
    CompressedBlock,
    ContentBlock,
    LogicalRange,
    Manifest,
)


def block(digest, refs, length=64):
    return ContentBlock(digest=digest, length=length, references=list(refs),
                        refcount=len(refs))


class CrossShardSharingTest(unittest.TestCase):
    """同一内容块被多个分片引用，一个压缩块即可覆盖全部范围。"""

    def test_shared_block_covers_all_shards(self):
        refs = [
            LogicalRange("shard-0", 0, 64),
            LogicalRange("shard-1", 128, 64),
            LogicalRange("shard-2", 0, 64),
        ]
        content = [block("digest-A", refs)]
        manifest = Manifest("m1", [CompressedBlock("cb-1", ("digest-A",))])
        report = CompactionValidator(content, [manifest]).validate()
        self.assertEqual(report.missing_ranges, [])
        self.assertEqual(report.conflicts, [])
        self.assertEqual(report.restorable_manifests, ["m1"])
        self.assertEqual(report.active_manifest, "m1")

    def test_partial_sharing_leaves_uncovered_ranges(self):
        content = [
            block("digest-A", [LogicalRange("shard-0", 0, 64)]),
            block("digest-B", [LogicalRange("shard-1", 0, 64)]),
        ]
        manifest = Manifest("m1", [CompressedBlock("cb-1", ("digest-A",))])
        report = CompactionValidator(content, [manifest]).validate()
        self.assertEqual(report.missing_ranges, [LogicalRange("shard-1", 0, 64)])
        self.assertEqual(report.restorable_manifests, [])


class CompressionInterruptionTest(unittest.TestCase):
    """压缩中断（未完整上传）的块不能算作有效覆盖。"""

    def test_incomplete_upload_counts_as_missing(self):
        content = [block("digest-A", [LogicalRange("shard-0", 0, 64)])]
        interrupted = CompressedBlock(
            "cb-1", ("digest-A",), uploaded=True,
            bytes_written=32, bytes_expected=128,
        )
        manifest = Manifest("m1", [interrupted])
        report = CompactionValidator(content, [manifest]).validate()
        self.assertEqual(report.missing_ranges, [LogicalRange("shard-0", 0, 64)])
        self.assertFalse(report.verdicts[0].committable)
        self.assertIn("压缩块未完整上传", report.verdicts[0].reasons[0])

    def test_not_uploaded_block_is_incomplete(self):
        content = [block("digest-A", [LogicalRange("shard-0", 0, 64)])]
        pending = CompressedBlock("cb-1", ("digest-A",), uploaded=False)
        manifest = Manifest("m1", [pending])
        report = CompactionValidator(content, [manifest]).validate()
        self.assertEqual(len(report.missing_ranges), 1)
        self.assertEqual(report.restorable_manifests, [])


class DuplicateDigestTest(unittest.TestCase):
    """重复摘要应去重合并；相同摘要的重叠引用不构成冲突。"""

    def test_duplicate_digests_merge_references(self):
        content = [
            block("digest-A", [LogicalRange("shard-0", 0, 64)]),
            block("digest-A", [LogicalRange("shard-1", 0, 64)]),
        ]
        manifest = Manifest("m1", [CompressedBlock("cb-1", ("digest-A",))])
        report = CompactionValidator(content, [manifest]).validate()
        self.assertEqual(report.missing_ranges, [])
        self.assertEqual(report.conflicts, [])
        self.assertEqual(report.restorable_manifests, ["m1"])

    def test_same_digest_overlap_is_not_a_conflict(self):
        content = [
            block("digest-A", [LogicalRange("shard-0", 0, 64)]),
            block("digest-A", [LogicalRange("shard-0", 32, 64)]),
        ]
        manifest = Manifest("m1", [CompressedBlock("cb-1", ("digest-A",))])
        report = CompactionValidator(content, [manifest]).validate()
        self.assertEqual(report.conflicts, [])

    def test_different_digest_overlap_is_a_conflict(self):
        content = [
            block("digest-A", [LogicalRange("shard-0", 0, 64)]),
            block("digest-B", [LogicalRange("shard-0", 32, 64)]),
        ]
        manifest = Manifest(
            "m1", [CompressedBlock("cb-1", ("digest-A", "digest-B"))]
        )
        report = CompactionValidator(content, [manifest]).validate()
        self.assertEqual(len(report.conflicts), 1)
        conflict = report.conflicts[0]
        self.assertEqual(conflict.range, LogicalRange("shard-0", 32, 32))
        self.assertEqual(conflict.digests, ("digest-A", "digest-B"))
        self.assertEqual(report.restorable_manifests, [])


class ManifestRollbackTest(unittest.TestCase):
    """新清单校验失败时，活动清单回滚到上一个可安全恢复的清单。"""

    def test_rollback_to_last_valid_manifest(self):
        content = [
            block("digest-A", [LogicalRange("shard-0", 0, 64)]),
            block("digest-B", [LogicalRange("shard-1", 0, 64)]),
        ]
        old = Manifest(
            "m-old",
            [
                CompressedBlock("cb-1", ("digest-A",)),
                CompressedBlock("cb-2", ("digest-B",)),
            ],
        )
        # 新清单只写入了部分压缩块，digest-B 无覆盖。
        new = Manifest("m-new", [CompressedBlock("cb-3", ("digest-A",))])
        validator = CompactionValidator(content, [old, new])
        report = validator.validate()
        self.assertEqual(report.restorable_manifests, ["m-old"])
        self.assertEqual(report.active_manifest, "m-old")
        self.assertTrue(old.committed)
        self.assertFalse(new.committed)
        self.assertEqual(
            report.verdicts[1].missing_ranges, [LogicalRange("shard-1", 0, 64)]
        )

    def test_no_valid_manifest_means_no_active(self):
        content = [block("digest-A", [LogicalRange("shard-0", 0, 64)])]
        broken = Manifest("m-broken", [])
        report = CompactionValidator(content, [broken]).validate()
        self.assertEqual(report.restorable_manifests, [])
        self.assertIsNone(report.active_manifest)


class CompletenessTest(unittest.TestCase):
    """完整覆盖 + 无冲突时清单可提交。"""

    def test_full_coverage_is_committable(self):
        content = [
            block("digest-A", [LogicalRange("shard-0", 0, 64)]),
            block("digest-B", [LogicalRange("shard-0", 64, 64)]),
            block("digest-C", [LogicalRange("shard-1", 0, 64)]),
        ]
        manifest = Manifest(
            "m1",
            [
                CompressedBlock("cb-1", ("digest-A", "digest-B")),
                CompressedBlock("cb-2", ("digest-C",)),
            ],
        )
        report = CompactionValidator(content, [manifest]).validate()
        self.assertEqual(report.missing_ranges, [])
        self.assertEqual(report.conflicts, [])
        self.assertTrue(report.verdicts[0].committable)
        self.assertEqual(report.active_manifest, "m1")


if __name__ == "__main__":
    unittest.main()
