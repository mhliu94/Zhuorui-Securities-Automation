package com.zhuorui.automation;

import com.android.uiautomator.core.Configurator;
import com.android.uiautomator.testrunner.UiAutomatorTestCase;
import java.io.File;

/** Dumps the active UI hierarchy without waiting for an idle accessibility window. */
public final class ZeroIdleHierarchyDumpTest extends UiAutomatorTestCase {
    private static final String DEFAULT_OUTPUT_FILE = "zhuorui-zero-idle-window.xml";

    public void testDumpHierarchyWithoutIdleWait() {
        String outputFile = getParams().getString("output");
        if (outputFile == null || !outputFile.matches("[A-Za-z0-9._-]+")) {
            outputFile = DEFAULT_OUTPUT_FILE;
        }
        Configurator.getInstance().setWaitForIdleTimeout(0L);
        File dumpedFile = new File("/data/local/tmp", outputFile);
        if (dumpedFile.exists() && !dumpedFile.delete()) {
            fail("Could not remove stale UI hierarchy " + dumpedFile);
        }
        for (int attempt = 0; attempt < 4; attempt++) {
            if (attempt > 0) {
                sleep(250L);
            }
            getUiDevice().dumpWindowHierarchy(outputFile);
            if (dumpedFile.isFile() && dumpedFile.length() > 0L) {
                return;
            }
        }
        fail("UI hierarchy was not written to " + dumpedFile);
    }
}
