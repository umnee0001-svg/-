#!/usr/bin/env bash
# 물때표 APK 빌드 (Gradle 없이 SDK 도구만 사용)
# 필요: JDK, ANDROID_HOME (platforms;android-35, build-tools;35.0.0)
set -euo pipefail
cd "$(dirname "$0")"
SDK="${ANDROID_HOME:-$HOME/android-sdk}"
BT="$SDK/build-tools/35.0.0"
JAR="$SDK/platforms/android-35/android.jar"
OUT=build
rm -rf "$OUT" && mkdir -p "$OUT/assets" "$OUT/classes" "$OUT/res-compiled"

# 1) 화면: 단독 HTML 파일을 앱 안에 넣는다
cp ../mulddae.html "$OUT/assets/index.html"

# 2) 리소스 컴파일 + 링크
"$BT/aapt2" compile --dir res -o "$OUT/res-compiled/res.zip"
"$BT/aapt2" link -o "$OUT/base.apk" -I "$JAR" --manifest AndroidManifest.xml \
  -A "$OUT/assets" --java "$OUT/gen" "$OUT/res-compiled/res.zip"

# 3) 자바 컴파일 → dex
javac -nowarn -Xlint:-options -source 8 -target 8 -bootclasspath "$JAR" -d "$OUT/classes" \
  $(find src "$OUT/gen" -name '*.java')
"$BT/d8" --min-api 24 --lib "$JAR" --output "$OUT" $(find "$OUT/classes" -name '*.class')
(cd "$OUT" && zip -q base.apk classes.dex)

# 4) 정렬 + 서명
KS="${KEYSTORE:-release.keystore}"
if [ ! -f "$KS" ]; then
  keytool -genkeypair -keystore "$KS" -alias mulddae -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass mulddae -keypass mulddae -dname "CN=Mulddae" >/dev/null 2>&1
fi
"$BT/zipalign" -f -p 4 "$OUT/base.apk" "$OUT/aligned.apk"
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:mulddae --key-pass pass:mulddae \
  --out ../mulddae.apk "$OUT/aligned.apk"
"$BT/apksigner" verify ../mulddae.apk && echo "완료: tide-app/mulddae.apk"
