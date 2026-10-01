"""CLI: tacio-operators --tenant tenants/dalla.toml <listar|executar|rodar>"""
import argparse
import logging
import sys

from ..config import carregar
from .executor import Agendador, Executor


def main() -> None:
    ap = argparse.ArgumentParser(prog="tacio-operators", description="Tacio Operators")
    ap.add_argument("--tenant", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("listar", help="Valida e lista os operators com a última execução")
    ex = sub.add_parser("executar", help="Executa um operator agora")
    ex.add_argument("id")
    ex.add_argument("--simular", action="store_true",
                    help="Mostra a mensagem sem enviar e sem gravar estado")
    sub.add_parser("rodar", help="Inicia o agendador (processo contínuo)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    executor = Executor(carregar(args.tenant))

    if args.cmd == "listar":
        for m in executor.manifestos():
            ult = executor.estado.ultima_execucao(m.id)
            situacao = "nunca executado" if not ult else (
                f"última: {ult['inicio']} | {ult['novos']} novos | "
                f"{'ERRO ' + ult['erro'] if ult['erro'] else 'ok'}")
            print(f"{'*' if m.ativo else ' '} {m.id:<24} {m.agenda.expr:<16} "
                  f"{m.tool:<26} -> {','.join(m.canais)}  [{situacao}]")
        return

    if args.cmd == "executar":
        m = next((x for x in executor.manifestos() if x.id == args.id), None)
        if not m:
            sys.exit(f"Operator '{args.id}' não encontrado.")
        rel = executor.executar(m, simular=args.simular)
        print(f"\n{rel.titulo}\n\n{rel.texto}\n")
        print(f"linhas={rel.linhas} novos={rel.novos} resolvidos={rel.resolvidos} "
              f"enviado={rel.enviado} erro={rel.erro}")
        sys.exit(1 if rel.erro else 0)

    Agendador(executor).rodar()


if __name__ == "__main__":
    main()
