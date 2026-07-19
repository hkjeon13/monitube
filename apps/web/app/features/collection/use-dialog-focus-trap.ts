import type { RefObject } from "react";
import { useEffect } from "react";

type DialogFocusTrapParams = {
  open: boolean;
  dialogRef: RefObject<HTMLElement | null>;
  onClose: () => void;
};

export function useDialogFocusTrap({ open, dialogRef, onClose }: DialogFocusTrapParams) {
  useEffect(() => {
    const dialog = open ? dialogRef.current : null;
    if (!dialog) return;

    const previousOverflow = document.body.style.overflow;
    const previousOverscrollBehavior = document.body.style.overscrollBehavior;
    document.body.style.overflow = "hidden";
    document.body.style.overscrollBehavior = "contain";

    const focusInitialControl = window.requestAnimationFrame(() => {
      const initialFocusTarget = dialog.querySelector<HTMLElement>("[data-drawer-initial-focus]") ?? dialog;
      initialFocusTarget.focus();
    });

    const keepFocusInDialog = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;

      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )).filter((element) => element.getAttribute("aria-hidden") !== "true");
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", keepFocusInDialog);
    return () => {
      window.cancelAnimationFrame(focusInitialControl);
      document.removeEventListener("keydown", keepFocusInDialog);
      document.body.style.overflow = previousOverflow;
      document.body.style.overscrollBehavior = previousOverscrollBehavior;
    };
  }, [dialogRef, onClose, open]);
}
