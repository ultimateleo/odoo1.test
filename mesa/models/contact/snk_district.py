from odoo import _, api, fields, models





class SnkDistrict(models.Model):
    _name = "snk.district"
    _description = "SNK District"
    _rec_name = 'snk_name'




    
    snk_name = fields.Char(string='Name')
    