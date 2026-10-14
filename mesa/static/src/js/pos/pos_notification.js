/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { onMounted, Component } from "@odoo/owl";

export class PosBusNotification extends Component {
    static template = owl.xml`<div/>`;
    static props = {};

    setup() {
        this.bus = useService("bus_service");
        this.notification = useService("notification");

        console.log(">>> [Mesa] POS Bus Notification (ImBus API) setup initialized");

        onMounted(() => {
            const channel = "snk_pos_sale_notification";
            
            // Make sure the channel is registered BEFORE start
            if (this.bus.addChannel(channel)) {
                console.log(">>> [Mesa] Registering channel:", channel);
            }

            if (!this.bus.running) {  // avoid restarting if already running
                this.bus.start();
                console.log(">>> [Mesa] WebSocket Bus started");
            } else {
                console.log(">>> [Mesa] WebSocket Bus running");
            }
            // if (this.bus.start) this.bus.start();

            // Subscribe to ImBus message event
            if (this.bus.subscribe) {
                console.log(">>> [Mesa] Subscribing to 'message' event");
                this.bus.subscribe(channel, (message) => this._onBusMessage(message));
            } else if (this.bus.addEventListener) {
                console.log(">>> [Mesa] Fallback addEventListener()");
                this.bus.addEventListener("message", (ev) => this._onBusMessage(ev.detail));
            }

            console.log(">>> [Mesa] POS Bus Notification ready ✅");

            // expose for debugging
            window.__mesa_bus__ = this.bus;
        });
    }

    _onBusMessage(event) {
        console.log("🟢 [Mesa] Received bus event:", event);

        try {
            const payload = event?.payload || event;
            if (payload?.message) {
                this.notification.add(payload.message, {
                    title: payload.title || "Notification",
                    type: payload.type || "info",
                });
            }
        } catch (err) {
            console.error("❌ [Mesa] Error handling bus event:", err, event);
        }
    }
}

registry.category("main_components").add("mesa_pos_bus_notification", {
    Component: PosBusNotification,
});
