import click

from glycan_profiling.cli.base import cli
from glycan_profiling.config.config_file import ( 
    add_user_modification_rule as add_user_peptide_modification_rule,
    add_user_substituent_rule)


from glycopeptidepy.structure.modification import extract_targets_from_string, ModificationRule, Composition


@cli.group(short_help='Set persistent configuration options')
def config():
    pass


def parse_peptide_modification_target_spec(context, spec_string_list):
    out = []
    for spec_string in spec_string_list:
        try:
            spec = extract_targets_from_string(spec_string)
            out.append(spec)
        except Exception as e:
            raise ValueError(str(e))
    return out


@config.command("add-peptide-modification")
@click.option("-n", '--name', required=True, help='Modification name')
@click.option("-c", "--composition", required=True, help='The chemical formula for this modification')
@click.option("-t", "--target", required=True, multiple=True,
              help="Target specification string of the form residue[@n-term|c-term]",
              callback=parse_peptide_modification_target_spec)
def peptide_modification(name, composition, target, categories=None):
    composition = Composition(str(composition))
    rule = ModificationRule(target, name, None, composition.mass, composition)
    add_user_peptide_modification_rule(rule)
    click.echo("Added %r to modification registry" % (rule,))


@config.command("add-substituent")
@click.option("-n", "--name", required=True, help='Substituent name')
@click.option("-c", "--composition", required=True, help='The chemical formula for this substituent')
@click.option("-s", "--is-nh-derivatizable", is_flag=True, help="Can this substituent be derivatized as at an N-H bond?")
@click.option("-d", "--can-nh-derivatize", is_flag=True, help="Will this substituent derivatize other substituents at sites like an N-H bond?")
@click.option("-a", "--attachment-loss", default="H", help="The composition lost by the parent molecule when this substituent is added. Defaults to \"H\"")
def substituent(name, composition, is_nh_derivatizable, can_nh_derivatize, attachment_loss):
    print(name)
    print(composition)
    print(is_nh_derivatizable)
    print(can_nh_derivatize)
    print(attachment_loss)

