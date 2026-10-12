package io.vpcore.app;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

/**
 * Код отказа PhoneActionsCore → статус итога голосовой команды: те же
 * правила, что у чата (failStatus и openApp в web/src/phone/actions.ts).
 */
public class PhoneVoiceOpsTest {

    @Test
    public void clockAndTorch() {
        assertEquals("no_handler", PhoneVoiceOps.failStatus("setTimer", "no_handler"));
        assertEquals("no_handler", PhoneVoiceOps.failStatus("torch", "no_handler"));
        assertEquals("busy", PhoneVoiceOps.failStatus("torch", "busy"));
        assertEquals("failed", PhoneVoiceOps.failStatus("setAlarm", "bad_args"));
        assertEquals("failed", PhoneVoiceOps.failStatus("showAlarms", "failed"));
        assertEquals("failed", PhoneVoiceOps.failStatus("setAlarm", "not_found"));
    }

    @Test
    public void openApp() {
        // Удалили между списком и запуском — «нет такого»; прочее — сбой
        assertEquals("not_found", PhoneVoiceOps.failStatus("openApp", "not_found"));
        assertEquals("failed", PhoneVoiceOps.failStatus("openApp", "no_handler"));
        assertEquals("failed", PhoneVoiceOps.failStatus("openApp", "bad_args"));
    }
}
