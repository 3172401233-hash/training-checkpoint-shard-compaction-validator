"""检查点分片压缩验证器。

在内容去重 + 压缩之后，校验提交清单（manifest）引用的压缩块是否
完整覆盖所有逻辑分片范围，且不存在重叠冲突。只处理元数据
（偏移、摘要、引用计数、上传状态），不加载真实权重。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True, order=True)
class LogicalRange:
    """逻辑分片内的一段字节范围。"""

    shard: str
    offset: int
    length: int

    @property
    def end(self) -> int:
        return self.offset + self.length

    def overlaps(self, other: "LogicalRange") -> bool:
        return (
            self.shard == other.shard
            and self.offset < other.end
            and other.offset < self.end
        )


@dataclass
class ContentBlock:
    """去重后的内容块，可被多个逻辑范围引用。"""

    digest: str
    length: int
    references: List[LogicalRange] = field(default_factory=list)
    refcount: int = 0


@dataclass
class CompressedBlock:
    """压缩块：打包了若干内容块，可能只上传了一部分。"""

    block_id: str
    digests: Tuple[str, ...]
    uploaded: bool = True
    bytes_written: int = 0
    bytes_expected: int = 0

    @property
    def complete(self) -> bool:
        if not self.uploaded:
            return False
        if self.bytes_expected and self.bytes_written < self.bytes_expected:
            return False
        return True


@dataclass
class Manifest:
    """提交清单：一组压缩块构成的可恢复视图。"""

    manifest_id: str
    compressed_blocks: List[CompressedBlock] = field(default_factory=list)
    committed: bool = False


@dataclass
class ConflictRecord:
    """同一逻辑范围被映射到不同摘要的冲突引用。"""

    range: LogicalRange
    digests: Tuple[str, ...]


@dataclass
class ManifestVerdict:
    manifest_id: str
    committable: bool
    missing_ranges: List[LogicalRange]
    conflicts: List[ConflictRecord]
    reasons: List[str]


@dataclass
class ValidationReport:
    missing_ranges: List[LogicalRange]
    conflicts: List[ConflictRecord]
    restorable_manifests: List[str]
    verdicts: List[ManifestVerdict]
    active_manifest: Optional[str]


class CompactionValidator:
    """校验压缩块对逻辑范围的覆盖完整性与一致性。"""

    def __init__(
        self,
        content_blocks: Sequence[ContentBlock],
        manifests: Sequence[Manifest],
    ) -> None:
        self._by_digest: Dict[str, ContentBlock] = {}
        for block in content_blocks:
            existing = self._by_digest.get(block.digest)
            if existing is None:
                self._by_digest[block.digest] = block
            else:
                # 重复摘要：去重合并引用，累计引用计数。
                existing.references.extend(block.references)
                existing.refcount += block.refcount
        self.manifests = list(manifests)

    def _all_references(self) -> List[Tuple[LogicalRange, str]]:
        refs: List[Tuple[LogicalRange, str]] = []
        for digest, block in self._by_digest.items():
            for ref in block.references:
                refs.append((ref, digest))
        return refs

    @staticmethod
    def _covered_digests(blocks: Sequence[CompressedBlock]) -> set:
        covered = set()
        for block in blocks:
            if block.complete:
                covered.update(block.digests)
        return covered

    @staticmethod
    def _find_conflicts(
        refs: Sequence[Tuple[LogicalRange, str]]
    ) -> List[ConflictRecord]:
        conflicts: Dict[LogicalRange, set] = {}
        ordered = sorted(refs, key=lambda item: item[0])
        for i in range(len(ordered)):
            ref_a, digest_a = ordered[i]
            for j in range(i + 1, len(ordered)):
                ref_b, digest_b = ordered[j]
                if ref_b.shard != ref_a.shard or ref_b.offset >= ref_a.end:
                    break
                if digest_a != digest_b and ref_a.overlaps(ref_b):
                    start = max(ref_a.offset, ref_b.offset)
                    key = LogicalRange(
                        ref_a.shard, start, min(ref_a.end, ref_b.end) - start
                    )
                    conflicts.setdefault(key, set()).update((digest_a, digest_b))
        return [
            ConflictRecord(range=key, digests=tuple(sorted(digests)))
            for key, digests in sorted(conflicts.items())
        ]

    def validate_manifest(self, manifest: Manifest) -> ManifestVerdict:
        refs = self._all_references()
        covered = self._covered_digests(manifest.compressed_blocks)
        missing = sorted({ref for ref, digest in refs if digest not in covered})
        conflicts = self._find_conflicts(refs)
        reasons: List[str] = []
        incomplete = [b.block_id for b in manifest.compressed_blocks if not b.complete]
        if incomplete:
            reasons.append("压缩块未完整上传: %s" % ", ".join(sorted(incomplete)))
        if missing:
            reasons.append("存在未被压缩块覆盖的逻辑范围")
        if conflicts:
            reasons.append("存在重叠冲突引用")
        return ManifestVerdict(
            manifest_id=manifest.manifest_id,
            committable=not reasons,
            missing_ranges=missing,
            conflicts=conflicts,
            reasons=reasons,
        )

    def validate(self) -> ValidationReport:
        verdicts = [self.validate_manifest(m) for m in self.manifests]
        refs = self._all_references()
        all_blocks = [
            block for manifest in self.manifests for block in manifest.compressed_blocks
        ]
        covered = self._covered_digests(all_blocks)
        missing = sorted({ref for ref, digest in refs if digest not in covered})
        conflicts = self._find_conflicts(refs)

        restorable: List[str] = []
        active: Optional[str] = None
        for manifest, verdict in zip(self.manifests, verdicts):
            if verdict.committable:
                manifest.committed = True
                restorable.append(manifest.manifest_id)
                active = manifest.manifest_id  # 取最后一个有效清单
            else:
                manifest.committed = False  # 校验失败，回滚到上一有效清单
        return ValidationReport(
            missing_ranges=missing,
            conflicts=conflicts,
            restorable_manifests=restorable,
            verdicts=verdicts,
            active_manifest=active,
        )
