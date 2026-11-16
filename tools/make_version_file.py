"""Erzeugt build/version_info.txt (Dateieigenschaften der exe) aus app/version.py."""
import os
import re

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(root, "app", "version.py"), encoding="utf-8").read()
version = re.search(r'VERSION\s*=\s*"([^"]+)"', src).group(1)
name = re.search(r'APP_NAME\s*=\s*"([^"]+)"', src).group(1)
nums = [int(x) for x in re.findall(r"\d+", version)][:4]
nums += [0] * (4 - len(nums))
tpl = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tuple(nums)}, prodvers={tuple(nums)}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040704B0', [
      StringStruct('FileDescription', 'System-Monitor MQTT - Systemwerte per MQTT senden'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{name}'),
      StringStruct('OriginalFilename', '{name}.exe'),
      StringStruct('ProductName', '{name}'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [0x0407, 1200])])
  ]
)
"""
os.makedirs(os.path.join(root, "build"), exist_ok=True)
with open(os.path.join(root, "build", "version_info.txt"), "w", encoding="utf-8") as f:
    f.write(tpl)
print(version)
