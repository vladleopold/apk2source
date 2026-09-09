#!/usr/bin/env python3
"""
Synthetic-but-real Unity fixture generator.

Builds genuine Unity binary containers (SerializedFile `.assets` and UnityFS
`.bundle`) that hold a Spine payload: TextAsset skeleton json, TextAsset atlas,
Texture2D page png, plus the spine-unity MonoBehaviour graph (MonoScript ->
SpineAtlasAsset / SkeletonDataAsset) and an AssetBundle container object.

Nothing here is taken from any shipped product: the skeleton JSON, the atlas and
the PNG are generated from scratch by this script, so the fixture is safe to
commit to a public repository and safe to run in CI.

Usage:
    python make_fixture.py --out ./build --unity 2021.3.16f1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import zlib
from typing import Any, Dict, List, Optional, Tuple

import UnityPy
from UnityPy.enums import BuildTarget, ClassIDType
from UnityPy.files.ObjectReader import ObjectReader
from UnityPy.files.SerializedFile import SerializedFile, SerializedType
from UnityPy.files.BundleFile import BundleFile
from UnityPy.helpers.Tpk import get_typetree_node
from UnityPy.helpers.TypeTreeHelper import write_typetree
from UnityPy.helpers.TypeTreeNode import TypeTreeNode
from UnityPy.helpers.UnityVersion import UnityVersion
from UnityPy.streams import EndianBinaryReader, EndianBinaryWriter

CAB_NAME = "CAB-4f2a9c1e8b7d3a6f0e5c9b1d7a3f6e20"
SKELETON_NAMES = ["spinebot", "spinebot_gear", "ui_banner"]
PAGE_W, PAGE_H = 128, 512


# ==========================================================================
# generated Spine payload (original content written for this harness)
# ==========================================================================
def make_rgba(width: int, height: int, seed: int) -> bytes:
    """Deterministic raw RGBA32 pixel data (what Unity stores inline)."""
    out = bytearray(width * height * 4)
    i = 0
    for y in range(height):
        for x in range(width):
            v = (zlib.crc32(struct.pack(">III", seed, x, y)) >> 7) & 0xFF
            out[i] = v
            out[i + 1] = (v * 7 + 40) & 0xFF
            out[i + 2] = (v * 3 + 120) & 0xFF
            out[i + 3] = 255
            i += 4
    return bytes(out)


def _vflip(rgba: bytes, w: int, h: int) -> bytes:
    """Unity stores textures bottom-up; UnityPy flips them on export."""
    row = w * 4
    return b"".join(rgba[(h - 1 - y) * row:(h - y) * row] for y in range(h))


def make_skeleton_json(name: str) -> str:
    bones: List[dict] = [{"name": "root"}]
    slots: List[dict] = []
    attachments: Dict[str, Any] = {}
    for i in range(6):
        bones.append({"name": f"bone{i}", "parent": "root", "x": i * 12.5, "y": i * -4.0})
        slots.append({"name": f"part{i}", "bone": f"bone{i}", "attachment": f"part{i}"})
        attachments[f"part{i}"] = {
            "x": 0, "y": 0, "width": 64, "height": 64,
            "u": 0, "v": 0, "u2": 1, "v2": 1, "path": f"{name}/part{i}.png",
        }
    anim = {
        "bones": {
            f"bone{i}": {
                "rotate": [{"time": 0, "angle": 0}, {"time": 0.5, "angle": 15 + i}, {"time": 1, "angle": 0}],
                "translate": [{"time": 0, "x": 0, "y": 0}, {"time": 1, "x": i, "y": -i}],
            }
            for i in range(6)
        },
        "slots": {
            f"part{i}": {"color": [{"time": 0, "color": "ffffffff"}, {"time": 1, "color": "ffffff88"}]}
            for i in range(6)
        },
    }
    doc = {
        "skeleton": {
            "hash": hashlib.sha1(name.encode()).hexdigest()[:16],
            "spine": "4.1.15",
            "x": -120.5, "y": -180.25, "width": 241.0, "height": 360.5,
            "fps": 30, "images": f"./{name}_images/",
        },
        "bones": bones,
        "slots": slots,
        "ik": [], "transform": [], "path": [],
        "skins": [{"name": "default", "attachments": attachments}],
        "events": {"footstep": {"int": 0, "float": 0, "string": ""}},
        "animations": {"idle": anim, "walk": anim, "jump": anim},
    }
    return json.dumps(doc, indent=2)


def make_atlas(name: str, page_w: int = PAGE_W, page_h: int = PAGE_H) -> str:
    lines = [f"{name}.png", f"size: {page_w},{page_h}", "format: RGBA8888",
             "filter: Linear,Linear", "repeat: none"]
    y = 2
    for i in range(6):
        lines += [f"part{i}", "  rotate: false", f"  xy: 2, {y}", "  size: 64, 64",
                  "  orig: 64, 64", "  offset: 0, 0", "  index: -1"]
        y += 66
    return "\n".join(lines) + "\n"


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b7 = n & 0x7F
        n >>= 7
        if n:
            out.append(b7 | 0x80)
        else:
            out.append(b7)
            return bytes(out)


def _spine_str(s: Optional[str]) -> bytes:
    """Spine SkeletonInput string: 0 -> null, 1 -> empty, else len+1 then bytes."""
    if s is None:
        return b"\x00"
    e = s.encode()
    return _varint(len(e) + 1) + e


def make_skel_binary(name: str) -> bytes:
    """A real Spine binary header followed by a synthetic body.

    The header (hash, version, width, height, nonessential) follows Spine's
    SkeletonBinary layout so binary-skeleton detectors can be exercised.
    The body is NOT a real Spine skeleton - it is a deterministic stub, since
    this fixture exists only to validate the extraction pipeline.
    """
    header = (
        _spine_str(None)
        + _spine_str("4.1.15")
        + struct.pack(">ff", 241.0, 360.5)
        + b"\x01"
    )
    body = make_skeleton_json(name).encode()
    return header + struct.pack(">I", len(body)) + body + b"\x00" * 32


# ==========================================================================
# Unity binary writing helpers
# ==========================================================================
def _hash16(*parts: str) -> bytes:
    return hashlib.md5("|".join(parts).encode()).digest()


def _hash128_dict(b: bytes) -> Dict[str, int]:
    return {f"bytes[{i}]": v for i, v in enumerate(b)}


def _pptr(path_id: int, file_id: int = 0) -> Dict[str, int]:
    return {"m_FileID": file_id, "m_PathID": path_id}


def _empty_serialized_file() -> bytes:
    w = EndianBinaryWriter()
    w.write_u_int_array([0, 0, 22, 0])
    w.write_boolean(False)
    w.write_bytes(b"\x00\x00\x00")
    w.write_u_int(0)
    w.write_long(0)
    w.write_long(0)
    w.write_long(0)
    w.write_string_to_null("2021.3.16f1")
    w.write_int(int(BuildTarget.Android))
    w.write_boolean(True)
    for _ in range(6):
        w.write_int(0)
    w.write_string_to_null("")
    return w.bytes


def normalize_tree(root: TypeTreeNode) -> TypeTreeNode:
    """Tpk-generated nodes miss m_TypeFlags/m_RefTypeHash; dump_blob needs ints."""
    for i, n in enumerate(root.traverse()):
        n.m_Index = i
        if n.m_TypeFlags is None:
            n.m_TypeFlags = 0
        if n.m_RefTypeHash is None:
            n.m_RefTypeHash = 0
        if n.m_VariableCount is None:
            n.m_VariableCount = 0
        if n.m_MetaFlag is None:
            n.m_MetaFlag = 0
    return root


def _node(level: int, typ: str, name: str, size: int, version: int,
          metaflag: int, children: Optional[List[TypeTreeNode]] = None) -> TypeTreeNode:
    n = TypeTreeNode(level, typ, name, size, version, children or [])
    n.m_MetaFlag = metaflag
    n.m_Index = 0
    n.m_TypeFlags = 0
    n.m_VariableCount = 0
    n.m_RefTypeHash = 0
    return n


def _string_node(level: int, name: str, metaflag: int = 0x00008001) -> TypeTreeNode:
    arr = _node(level + 1, "Array", "Array", -1, 1, 0x00040001, [
        _node(level + 2, "int", "size", 4, 1, 0x00000001),
        _node(level + 2, "char", "data", 1, 1, 0x00000001),
    ])
    return _node(level, "string", name, -1, 1, 0x00008000, [arr])


def _pptr_node(level: int, name: str, target: str = "Object") -> TypeTreeNode:
    return _node(level, f"PPtr<{target}>", name, 12, 1, 0x00000000, [
        _node(level + 1, "int", "m_FileID", 4, 1, 0x00000000),
        _node(level + 1, "SInt64", "m_PathID", 8, 1, 0x00000000),
    ])


def _vector_node(level: int, name: str, item: TypeTreeNode, metaflag: int = 0x00008000) -> TypeTreeNode:
    arr = _node(level + 1, "Array", "Array", -1, 1, 0x00004000, [
        _node(level + 2, "int", "size", 4, 1, 0x00000000),
        item,
    ])
    return _node(level, "vector", name, -1, 1, metaflag, [arr])


def _spine_monobehaviour_typetree() -> TypeTreeNode:
    """Typetree for spine-unity SkeletonDataAsset / SpineAtlasAsset MonoBehaviours."""
    return _node(0, "MonoBehaviour", "Base", -1, 3, 0x00008000, [
        _pptr_node(1, "m_GameObject", "GameObject"),
        _node(1, "bool", "m_Enabled", 1, 2, 0x00000000),
        _pptr_node(1, "m_Script", "MonoScript"),
        _string_node(1, "m_Name"),
        _vector_node(2, "atlasAssets", _pptr_node(3, "data", "Object")),
        _pptr_node(1, "skeletonJSON", "Object"),
        _vector_node(1, "atlasFile", _node(2, "char", "data", 1, 1, 0x00000000)),
        _node(1, "float", "scale", 4, 1, 0x00000000),
        _vector_node(1, "fromAnimation", _string_node(2, "data")),
        _vector_node(1, "toAnimation", _string_node(2, "data")),
    ])


class _RawFile:
    """Shim accepted by BundleFile.save_fs (needs .bytes/.flags/.save())."""

    def __init__(self, data: bytes):
        self.bytes = data
        self.flags = 0

    def save(self, packer: Optional[str] = None) -> bytes:
        return self.bytes


def _make_serialized_type(sf: SerializedFile, cid: int, node: TypeTreeNode,
                          unity_version: str, script_id: Optional[bytes] = None) -> SerializedType:
    st = SerializedType.__new__(SerializedType)
    st.__attrs_init__(cid)
    st.is_stripped_type = False
    st.script_type_index = -1
    if script_id is None and cid == 114:
        script_id = _hash16("script", "MonoBehaviour", unity_version)
    st.script_id = script_id
    st.old_type_hash = _hash16("oldhash", str(cid), unity_version, (script_id or b"").hex())
    st.node = node
    st.m_ClassName = None
    st.m_NameSpace = None
    st.m_AssemblyName = None
    st.type_dependencies = (cid,)
    return st


def build_serialized_file(unity_version: str, platform: BuildTarget,
                          objects: List[Tuple[int, int, Dict[str, Any], Optional[TypeTreeNode]]],
                          extra_types: Optional[List[Tuple[int, TypeTreeNode, Optional[bytes]]]] = None,
                          ) -> bytes:
    """objects: (path_id, class_id, payload, optional custom typetree)."""
    ver = UnityVersion.from_str(unity_version)
    sf = SerializedFile(EndianBinaryReader(_empty_serialized_file()), name="sharedassets0.assets")
    sf.unity_version = unity_version
    sf.set_version(unity_version)
    sf._m_target_platform = int(platform)
    sf.target_platform = platform
    sf._enable_type_tree = True
    sf.userInformation = ""
    sf.ref_types = []
    sf.script_types = []
    sf.externals = []
    sf.big_id_enabled = 0

    types: List[SerializedType] = []
    index_of: Dict[Tuple[int, Optional[bytes]], int] = {}

    def add_type(cid: int, node: TypeTreeNode, script_id: Optional[bytes] = None) -> int:
        key = (cid, script_id)
        if key in index_of:
            return index_of[key]
        types.append(_make_serialized_type(sf, cid, node, unity_version, script_id))
        index_of[key] = len(types) - 1
        return index_of[key]

    for cid, node, script_id in (extra_types or []):
        add_type(cid, node, script_id)

    readers: Dict[int, ObjectReader] = {}
    for path_id, cid, payload, custom_node in objects:
        node = custom_node if custom_node is not None else normalize_tree(get_typetree_node(cid, ver))
        script_id = None
        if cid == 114 and custom_node is not None:
            script_id = _hash16("script", "spine-unity", str(payload.get("m_Name", "")).split("_")[-1])
        w = EndianBinaryWriter(endian="<")
        write_typetree(payload, node, w, sf)
        data = w.bytes
        ti = add_type(cid, node, script_id)
        r = ObjectReader(
            assets_file=sf, reader=None, path_id=path_id, type_id=ti,
            serialized_type=types[ti], class_id=cid, type=ClassIDType(cid),
            byte_start=0, byte_size=len(data), is_destroyed=None, is_stripped=None,
        )
        r.data = data
        readers[path_id] = r

    sf.types = types
    sf.objects = readers
    return sf.save()


def wrap_unityfs(sf_bytes: bytes, engine_version: str, packer: str = "none",
                 cab_name: str = CAB_NAME) -> bytes:
    """Wrap raw SerializedFile bytes into a real UnityFS bundle."""
    blockinfo = b"\x00" * 16 + struct.pack("<i", 0) + struct.pack("<i", 0)
    w = EndianBinaryWriter()
    w.write_string_to_null("UnityFS")
    w.write_u_int(6)
    w.write_string_to_null("5.x.x")
    w.write_string_to_null(engine_version)
    w.write_long(0)
    w.write_u_int(len(blockinfo))   # compressed blockinfo size
    w.write_u_int(len(blockinfo))   # uncompressed blockinfo size
    w.write_u_int(0x40)             # flags: BlocksInfoAtTheEnd, uncompressed
    w.write(blockinfo)

    bundle = BundleFile(EndianBinaryReader(w.bytes), parent=None, name="spine.bundle")
    bundle.files = {cab_name: _RawFile(sf_bytes)}
    return bundle.save(packer=packer)


# ==========================================================================
# fixture assembly
# ==========================================================================
def _pid(tag: str) -> int:
    return 1000 + (zlib.crc32(tag.encode()) % 800000)


def build_all(out_dir: str, unity_version: str = "2021.3.16f1") -> Dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    platform = BuildTarget.Android
    mb_node = _spine_monobehaviour_typetree()

    objects: List[Tuple[int, int, Dict[str, Any], Optional[TypeTreeNode]]] = []
    container: Dict[str, int] = {}
    expect: Dict[str, Any] = {}

    # AssetBundle container object lives at path_id 1
    for name in SKELETON_NAMES:
        rgba = make_rgba(PAGE_W, PAGE_H, zlib.crc32(name.encode()) & 0xFFFF)
        skeleton = make_skeleton_json(name)
        atlas = make_atlas(name)
        skel_bin = make_skel_binary(name)

        pid_tex, pid_json, pid_atlas = _pid(name + "tex"), _pid(name + "json"), _pid(name + "atlas")
        pid_skel, pid_go, pid_mat, pid_script = (
            _pid(name + "skel"), _pid(name + "go"), _pid(name + "mat"), _pid(name + "script"))
        pid_atlasasset, pid_sda = _pid(name + "atlasasset"), _pid(name + "sda")

        objects.append((pid_tex, int(ClassIDType.Texture2D), {
            "m_Name": name,
            "m_ForcedFallbackFormat": 4,
            "m_DownscaleFallback": False,
            "m_IsAlphaChannelOptional": False,
            "m_Width": PAGE_W,
            "m_Height": PAGE_H,
            "m_CompleteImageSize": len(rgba),
            "m_MipsStripped": 0,
            "m_TextureFormat": 4,
            "m_MipCount": 1,
            "m_IsReadable": True,
            "m_IsPreProcessed": False,
            "m_IgnoreMasterTextureLimit": False,
            "m_StreamingMipmaps": False,
            "m_StreamingMipmapsPriority": 0,
            "m_ImageCount": 1,
            "m_TextureDimension": 2,
            "m_TextureSettings": {"m_FilterMode": 1, "m_Aniso": 1, "m_MipBias": 0.0,
                                  "m_WrapU": 0, "m_WrapV": 0, "m_WrapW": 0},
            "m_LightmapFormat": 0,
            "m_ColorSpace": 0,
            "m_PlatformBlob": [],
            "image data": rgba,
            "m_StreamData": {"offset": 0, "size": 0, "path": ""},
        }, None))

        objects.append((pid_json, int(ClassIDType.TextAsset),
                        {"m_Name": name, "m_Script": skeleton}, None))
        objects.append((pid_atlas, int(ClassIDType.TextAsset),
                        {"m_Name": f"{name}.atlas", "m_Script": atlas}, None))
        objects.append((pid_skel, int(ClassIDType.TextAsset),
                        {"m_Name": f"{name}_SkeletonData",
                         "m_Script": skel_bin.decode("utf-8", "surrogateescape")}, None))

        objects.append((pid_script, int(ClassIDType.MonoScript), {
            "m_Name": "SkeletonDataAsset",
            "m_ExecutionOrder": 0,
            "m_PropertiesHash": _hash128_dict(_hash16("props", name)),
            "m_ClassName": "SkeletonDataAsset",
            "m_Namespace": "Spine.Unity",
            "m_AssemblyName": "spine-unity.dll",
        }, None))

        objects.append((pid_mat, int(ClassIDType.GameObject), {
            "m_Component": [{"component": _pptr(pid_sda)}, {"component": _pptr(pid_atlasasset)}],
            "m_Layer": 0,
            "m_Name": f"{name}_GO",
            "m_Tag": 0,
            "m_IsActive": True,
        }, None))

        objects.append((pid_atlasasset, 114, {
            "m_GameObject": _pptr(pid_mat),
            "m_Enabled": True,
            "m_Script": _pptr(pid_script),
            "m_Name": f"{name}_AtlasAsset",
            "atlasAssets": [],
            "skeletonJSON": _pptr(0),
            "atlasFile": list(atlas.encode()),
            "scale": 0.01,
            "fromAnimation": [],
            "toAnimation": [],
        }, mb_node))

        objects.append((pid_sda, 114, {
            "m_GameObject": _pptr(pid_mat),
            "m_Enabled": True,
            "m_Script": _pptr(pid_script),
            "m_Name": f"{name}_SkeletonDataAsset",
            "atlasAssets": [_pptr(pid_atlasasset)],
            "skeletonJSON": _pptr(pid_json),
            "atlasFile": [],
            "scale": 0.01,
            "fromAnimation": ["idle", "walk"],
            "toAnimation": ["idle", "walk"],
        }, mb_node))

        container.update({
            f"assets/spine/{name}.png": pid_tex,
            f"assets/spine/{name}.json": pid_json,
            f"assets/spine/{name}.atlas.txt": pid_atlas,
            f"assets/spine/{name}_SkeletonData.bytes": pid_skel,
        })
        expect[name] = {
            "json_sha256": hashlib.sha256(skeleton.encode()).hexdigest(),
            "json_bytes": len(skeleton.encode()),
            "atlas_sha256": hashlib.sha256(atlas.encode()).hexdigest(),
            "atlas_bytes": len(atlas.encode()),
            "skel_sha256": hashlib.sha256(skel_bin).hexdigest(),
            "rgba_sha256": hashlib.sha256(rgba).hexdigest(),
            "rgba_sha256_flipped": hashlib.sha256(_vflip(rgba, PAGE_W, PAGE_H)).hexdigest(),
            "rgba_bytes": len(rgba),
            "png_size": [PAGE_W, PAGE_H],
            "texture_format": 4,
            "animations": ["idle", "walk", "jump"],
            "bones": 7,
            "slots": 6,
        }

    # AssetBundle container object (path_id 1)
    ab_node = normalize_tree(get_typetree_node(int(ClassIDType.AssetBundle), UnityVersion.from_str(unity_version)))
    objects.insert(0, (1, int(ClassIDType.AssetBundle), {
        "m_Name": "spine",
        "m_PreloadTable": [],
        "m_Container": [
            (path, {"preloadIndex": i, "preloadSize": 1, "asset": _pptr(pid)})
            for i, (path, pid) in enumerate(container.items())
        ],
        "m_MainAsset": {"preloadIndex": 0, "preloadSize": 0, "asset": _pptr(0)},
        "m_RuntimeCompatibility": 1,
        "m_AssetBundleName": "spine_assets",
        "m_Dependencies": [],
        "m_IsStreamedSceneAssetBundle": False,
        "m_ExplicitDataLayout": 0,
        "m_PathFlags": 0,
        "m_SceneHashes": [],
    }, ab_node))

    sf_bytes = build_serialized_file(unity_version, platform, objects)

    outputs: Dict[str, bytes] = {
        "sharedassets0.assets": sf_bytes,
        "spine_assets.bundle": wrap_unityfs(sf_bytes, unity_version, "none"),
        "spine_assets_lz4.bundle": wrap_unityfs(sf_bytes, unity_version, "lz4"),
    }
    for fname, blob in outputs.items():
        with open(os.path.join(out_dir, fname), "wb") as f:
            f.write(blob)

    manifest = {
        "unity_version": unity_version,
        "cab": CAB_NAME,
        "skeletons": SKELETON_NAMES,
        "files": {k: len(v) for k, v in outputs.items()},
        "expect": expect,
    }
    with open(os.path.join(out_dir, "fixture_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./build/fixture")
    ap.add_argument("--unity", default="2021.3.16f1")
    a = ap.parse_args()
    print(json.dumps(build_all(a.out, a.unity), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
