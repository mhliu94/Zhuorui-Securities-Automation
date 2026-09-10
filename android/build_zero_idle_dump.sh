#!/usr/bin/env bash
set -euo pipefail

SCRIPT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
OUTPUT_PATH="$SCRIPT_ROOT/zhuorui-zero-idle-dump.jar"
SDK_ROOT=""
JAVA_HOME_PATH=""
JUNIT_JAR=""

while (( $# )); do
    case $1 in
        --output-path|-o) OUTPUT_PATH=${2:?Missing value for $1}; shift 2 ;;
        --sdk-root) SDK_ROOT=${2:?Missing value for $1}; shift 2 ;;
        --java-home) JAVA_HOME_PATH=${2:?Missing value for $1}; shift 2 ;;
        --junit-jar) JUNIT_JAR=${2:?Missing value for $1}; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--output-path PATH] [--sdk-root PATH] [--java-home PATH] [--junit-jar PATH]"
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

if [[ -z $SDK_ROOT ]]; then
    for CANDIDATE in "${ANDROID_SDK_ROOT:-}" "${ANDROID_HOME:-}" "${HOME:-}/Android/Sdk"; do
        if [[ -n $CANDIDATE && -d $CANDIDATE ]]; then
            SDK_ROOT=$CANDIDATE
            break
        fi
    done
fi
[[ -n $SDK_ROOT ]] || { echo "No Android SDK root was found. Pass --sdk-root or set ANDROID_SDK_ROOT." >&2; exit 1; }

PLATFORM=""
while IFS= read -r CANDIDATE; do
    if [[ -f $CANDIDATE/android.jar && -f $CANDIDATE/uiautomator.jar ]]; then
        PLATFORM=$CANDIDATE
        break
    fi
done < <(find "$SDK_ROOT/platforms" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | sort -V -r)
[[ -n $PLATFORM ]] || { echo "No Android SDK platform with android.jar and uiautomator.jar was found." >&2; exit 1; }

BUILD_TOOLS=""
while IFS= read -r CANDIDATE; do
    if [[ -x $CANDIDATE/d8 ]]; then
        BUILD_TOOLS=$CANDIDATE
        break
    fi
done < <(find "$SDK_ROOT/build-tools" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | sort -V -r)
[[ -n $BUILD_TOOLS ]] || { echo "No Android SDK build-tools installation with d8 was found." >&2; exit 1; }

if [[ -z $JAVA_HOME_PATH ]]; then
    if [[ -n ${JAVA_HOME:-} && -x ${JAVA_HOME}/bin/javac ]]; then
        JAVA_HOME_PATH=$JAVA_HOME
    elif command -v javac >/dev/null 2>&1; then
        JAVAC_COMMAND=$(readlink -f "$(command -v javac)")
        JAVA_HOME_PATH=$(cd -- "$(dirname -- "$JAVAC_COMMAND")/.." && pwd)
    elif [[ -x /opt/android-studio/jbr/bin/javac ]]; then
        JAVA_HOME_PATH=/opt/android-studio/jbr
    fi
fi
[[ -n $JAVA_HOME_PATH && -x $JAVA_HOME_PATH/bin/javac ]] || {
    echo "No Java development kit was found. Pass --java-home or set JAVA_HOME." >&2
    exit 1
}
if [[ -z $JUNIT_JAR ]]; then
    for CANDIDATE in \
        "$JAVA_HOME_PATH/../lib/junit4.jar" \
        /opt/android-studio/lib/junit4.jar \
        "${HOME:-}/android-studio/lib/junit4.jar" \
        /usr/share/java/junit4.jar
    do
        if [[ -f $CANDIDATE ]]; then
            JUNIT_JAR=$CANDIDATE
            break
        fi
    done
fi
[[ -n $JUNIT_JAR && -f $JUNIT_JAR ]] || {
    echo "JUnit 4 was not found. Pass --junit-jar with the Android Studio JUnit jar." >&2
    exit 1
}

SOURCE_PATH="$SCRIPT_ROOT/ZeroIdleHierarchyDumpTest.java"
[[ -f $SOURCE_PATH ]] || { echo "Java source not found: $SOURCE_PATH" >&2; exit 1; }
TEMP_BASE=${TMPDIR:-/tmp}
TEMP_ROOT=$(mktemp -d "$TEMP_BASE/zhuorui-zero-idle.XXXXXX")
cleanup() {
    case $TEMP_ROOT in
        "$TEMP_BASE"/zhuorui-zero-idle.*) rm -rf -- "$TEMP_ROOT" ;;
        *) echo "Refusing to remove unexpected temporary path: $TEMP_ROOT" >&2 ;;
    esac
}
trap cleanup EXIT
CLASSES_PATH="$TEMP_ROOT/classes"
BUILT_JAR="$TEMP_ROOT/zhuorui-zero-idle-dump.jar"
mkdir -p -- "$CLASSES_PATH"

ANDROID_JAR="$PLATFORM/android.jar"
UIAUTOMATOR_JAR="$PLATFORM/uiautomator.jar"
export JAVA_HOME="$JAVA_HOME_PATH"
"$JAVA_HOME_PATH/bin/javac" -source 8 -target 8 \
    -classpath "$ANDROID_JAR:$UIAUTOMATOR_JAR:$JUNIT_JAR" \
    -d "$CLASSES_PATH" "$SOURCE_PATH"
CLASS_FILE="$CLASSES_PATH/com/zhuorui/automation/ZeroIdleHierarchyDumpTest.class"
[[ -f $CLASS_FILE ]] || { echo "Expected class file was not generated: $CLASS_FILE" >&2; exit 1; }
"$BUILD_TOOLS/d8" --lib "$ANDROID_JAR" --lib "$UIAUTOMATOR_JAR" --lib "$JUNIT_JAR" \
    --output "$BUILT_JAR" "$CLASS_FILE"
mkdir -p -- "$(dirname -- "$OUTPUT_PATH")"
cp -f -- "$BUILT_JAR" "$OUTPUT_PATH"
echo "Built $OUTPUT_PATH"
