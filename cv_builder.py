#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cv_builder.py

Construit le CV .docx a partir d'une structure de donnees.
Format strictement ATS-safe :
  une colonne, sans photo, sans icone, sans tableau,
  sans en-tete ni pied de page Word (beaucoup d'ATS les ignorent),
  puces Word natives, dates en MM/AAAA, Calibri noir aligne a gauche,
  metadonnees propres.

Le .docx est le format a deposer sur les portails : Workday, Taleo et
SuccessFactors le parsent mieux que le PDF et alimentent leur autofill avec.
"""

from docx import Document
from docx.shared import Pt, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

POLICE = 'Calibri'
GRIS = RGBColor(0x40, 0x40, 0x40)
BLEU = RGBColor(0x2D, 0x4A, 0x6B)


def _bordure_bas(par, couleur='808080'):
    pPr = par._p.get_or_add_pPr()
    bdr = OxmlElement('w:pBdr')
    bottom = OxmlElement('w:bottom')
    bottom.set(qn('w:val'), 'single')
    bottom.set(qn('w:sz'), '6')
    bottom.set(qn('w:space'), '3')
    bottom.set(qn('w:color'), couleur)
    bdr.append(bottom)
    pPr.append(bdr)


class CV:
    def __init__(self):
        self.doc = Document()
        s = self.doc.sections[0]
        s.top_margin = s.bottom_margin = Cm(1.3)
        s.left_margin = s.right_margin = Cm(1.5)
        n = self.doc.styles['Normal']
        n.font.name = POLICE
        n.font.size = Pt(10.5)
        n.paragraph_format.space_after = Pt(3)
        n.paragraph_format.line_spacing = 1.05

    def p(self, texte='', taille=10.5, gras=False, italique=False,
          couleur=None, apres=3):
        par = self.doc.add_paragraph()
        par.alignment = WD_ALIGN_PARAGRAPH.LEFT
        par.paragraph_format.space_after = Pt(apres)
        if texte:
            r = par.add_run(texte)
            r.font.name = POLICE
            r.font.size = Pt(taille)
            r.bold = gras
            r.italic = italique
            if couleur:
                r.font.color.rgb = couleur
        return par

    def section(self, titre):
        par = self.p(titre, taille=11.5, gras=True, couleur=BLEU, apres=5)
        par.paragraph_format.space_before = Pt(10)
        _bordure_bas(par)
        return par

    def puce(self, texte, gras=False):
        par = self.doc.add_paragraph(style='List Bullet')
        par.paragraph_format.space_after = Pt(2)
        par.paragraph_format.left_indent = Cm(0.55)
        par.paragraph_format.first_line_indent = Cm(-0.35)
        r = par.add_run(texte)
        r.font.name = POLICE
        r.font.size = Pt(10.5)
        r.bold = gras
        return par

    def etiquette(self, label, contenu):
        par = self.doc.add_paragraph()
        par.paragraph_format.space_after = Pt(3)
        a = par.add_run(f'{label} : ')
        a.font.name, a.font.size, a.bold = POLICE, Pt(10.5), True
        b = par.add_run(contenu)
        b.font.name, b.font.size = POLICE, Pt(10.5)
        return par

    def enregistrer(self, chemin, auteur='Mehdi Koriche'):
        cp = self.doc.core_properties
        cp.author = auteur
        cp.last_modified_by = auteur
        cp.title = f'CV {auteur}'
        cp.comments = ''
        cp.category = ''
        self.doc.save(chemin)
        return chemin


def construire(d, chemin):
    """
    d = {
      'nom', 'titre', 'contact': [...], 'accroche',
      'experiences': [{'intitule','employeur','contexte','puces':[],'perf'}],
      'competences': [(label, contenu)],
      'outils':      [(label, contenu)],
      'formation':   [{'diplome','ecole','contenu'}],
      'langues': '...'
    }
    """
    cv = CV()

    cv.p(d['nom'], taille=16, gras=True, apres=2)
    cv.p(d['titre'], taille=11.5, gras=True, apres=3)
    for ligne in d['contact']:
        cv.p(ligne, apres=1)
    cv.p(apres=5)

    cv.section('Profil')
    cv.p(d['accroche'])

    cv.section('Expérience professionnelle')
    for i, e in enumerate(d['experiences']):
        if i:
            cv.p(apres=4)
        cv.p(e['intitule'], gras=True, apres=1)
        cv.p(e['employeur'], couleur=GRIS, apres=3)
        if e.get('contexte'):
            cv.p(e['contexte'], italique=True, couleur=GRIS, apres=3)
        for b in e.get('puces', []):
            cv.puce(b)
        if e.get('perf'):
            cv.puce(e['perf'], gras=True)

    cv.section('Compétences')
    for label, contenu in d['competences']:
        cv.etiquette(label, contenu)

    cv.section('Outils et logiciels')
    for label, contenu in d['outils']:
        cv.etiquette(label, contenu)

    cv.section('Formation')
    for f in d['formation']:
        cv.p(f['diplome'], gras=True, apres=1)
        cv.p(f['ecole'], couleur=GRIS, apres=2)
        if f.get('contenu'):
            cv.p(f['contenu'], apres=5)

    cv.section('Langues')
    cv.p(d['langues'])

    return cv.enregistrer(chemin)
