from odoo import api, fields, models, _


class ChooseDeliveryPackage(models.TransientModel):
    _inherit = 'choose.delivery.package'

    def action_put_in_pack(self):
        self.ensure_one()
        ctx = dict(self.env.context)
        if self.delivery_package_type_id:
            ctx['default_package_type_id'] = self.delivery_package_type_id.id

        return super(ChooseDeliveryPackage, self.with_context(ctx)).action_put_in_pack()