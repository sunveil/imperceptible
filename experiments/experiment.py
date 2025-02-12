#!/usr/bin/env python3
from experiments.defence import defence
from experiments.toxic.core.model import ModelWrapper

try:
  import sys
  import torch
  import pickle
  import json
  import numpy as np
  import pandas as pd
  from bs4 import BeautifulSoup
  from timeit import timeit
  from abc import ABC
  from typing import List, Tuple, Callable, Dict
  from fairseq.hub_utils import GeneratorHubInterface
  from scipy.optimize import differential_evolution
  from textdistance import levenshtein
  from tqdm.auto import tqdm
  from argparse import ArgumentParser
  from sacrebleu import corpus_bleu
  from time import process_time, sleep, time
  from torch.nn.functional import softmax
  from os.path import exists
  #from toxic.core.model import ModelWrapper
  from logging import getLogger, WARNING
  from googleapiclient.discovery_cache.base import Cache
  from googleapiclient import discovery
  from getpass import getpass
  from transformers import Pipeline, pipeline
  from datasets import load_dataset
  from string import punctuation

  # --- Constants ---

  # Zero width space
  ZWSP = chr(0x200B)
  # Zero width joiner
  ZWJ = chr(0x200D)
  # Zero width non-joiner
  ZWNJ = chr(0x200C)
  # Unicode Bidi override characters
  PDF = chr(0x202C)
  LRE = chr(0x202A)
  RLE = chr(0x202B)
  LRO = chr(0x202D)
  RLO = chr(0x202E)
  PDI = chr(0x2069)
  LRI = chr(0x2066)
  RLI = chr(0x2067)
  # Backspace character
  BKSP = chr(0x8)
  # Delete character
  DEL = chr(0x7F)
  # Carriage return character
  CR = chr(0xD)
  NLP_DEFENCE = False
  # Load Unicode Intentional homoglyph characters
  intentionals = dict()
  with open("intentional.txt", "r") as f:
    for line in f.readlines():
      if len(line.strip()):
        if line[0] != '#':
          line = line.replace("#*", "#")
          _, line = line.split("#", maxsplit=1)
          if line[3] not in intentionals:
            intentionals[line[3]] = []
          intentionals[line[3]].append(line[7])

  label_map = {'contradiction': 0, 'neutral': 1, 'entailment': 2}
  with open('multinli_1.0/multinli_1.0_dev_matched.jsonl', 'r') as f:
    mnli_test = []
    for jline in f.readlines():
      line = json.loads(jline)
      if line['gold_label'] in label_map:
        mnli_test.append(line)

  # Load toxic comments data set
  getLogger('numexpr.utils').setLevel(WARNING)
  comments = pd.read_csv('toxicity_annotated_comments.tsv', sep = '\t', index_col = 0)
  annotations = pd.read_csv('toxicity_annotations.tsv',  sep = '\t')
  # labels a comment as toxic if the majority of annoatators did so
  labels = annotations.groupby('rev_id')['toxicity'].mean() > 0.5
  # join labels and comments
  comments['toxicity'] = labels
  # remove newline and tab tokens
  comments['comment'] = comments['comment'].apply(lambda x: x.replace("NEWLINE_TOKEN", " "))
  comments['comment'] = comments['comment'].apply(lambda x: x.replace("TAB_TOKEN", " "))
  test_comments = comments.query("split=='test'").query("toxicity==True")
  examples = test_comments.reset_index().to_dict('records')

  ner_labels = [ 'O', 'B-PER', 'I-PER', 'B-ORG', 'I-ORG', 'B-LOC', 'I-LOC', 'B-MISC', 'I-MISC']
  ner_classes = ['PER', 'ORG', 'LOC', 'MISC']

  emotion_classes = ['sadness', 'joy', 'love', 'anger', 'fear', 'surprise']

except Exception as ex:
  print("Unable to initiate. Please ensure that you have already run `setup.sh` in this environment.")
  sys.exit(ex)


# --- Classes ---

class Swap():
    """Represents swapped elements in a string of text."""
    def __init__(self, one, two):
        self.one = one
        self.two = two
    
    def __repr__(self):
        return f"Swap({self.one}, {self.two})"

    def __eq__(self, other):
        return self.one == other.one and self.two == other.two

    def __hash__(self):
        return hash((self.one, self.two))


class Objective(ABC):
  """ Abstract class representing objectives for scipy's genetic algorithms."""

  def __init__(self, model: GeneratorHubInterface, input: str, ref_translation: str, max_perturbs: int, distance: Callable[[str,str],int]):
    if not model:
      raise ValueError("Must supply model.")
    if not input:
      raise ValueError("Must supply input.")

    self.model: GeneratorHubInterface = model
    self.input: str = input
    self.ref_translation = ref_translation
    self.max_perturbs: int = max_perturbs
    self.distance: Callable[[str,str],int] = distance
    self.output = self.model.translate(self.input)

  def objective(self) -> Callable[[List[float]], float]:
    def _objective(perturbations: List[float]) -> float:
      candidate: str = self.candidate(perturbations)
      translation: str = self.model.translate(candidate)
      return -self.distance(self.output, translation)
    return _objective

  def differential_evolution(self, verbose=False, maxiter=60, popsize=32, polish=False) -> str:
    start = process_time()
    result = differential_evolution(self.objective(), self.bounds(),
                                    disp=verbose, maxiter=maxiter,
                                    popsize=popsize, polish=polish)
    end = process_time()
    candidate = self.candidate(result.x)
    translation = self.model.translate(candidate)
    return  {
              'adv_example': candidate,
              'adv_example_enc': result.x,
              'adv_translation': translation,
              'input_translation_distance': -result.fun,
              'ref_translation_distance': self.distance(translation, self.ref_translation),
              'input': self.input,
              'input_translation': self.output,
              'adv_translation': self.model.translate(candidate),
              'ref_translation': self.ref_translation,
              'ref_bleu': corpus_bleu(translation, self.ref_translation).score,
              'input_bleu': corpus_bleu(translation, self.output).score,
              'adv_generation_time': end - start,
              'budget': self.max_perturbs,
              'maxiter': maxiter,
              'popsize': popsize
            }

  def bounds(self) -> List[Tuple[float, float]]:
    raise NotImplementedError()

  def candidate(self, perturbations: List[float]) -> str:
    raise NotImplementedError()


class InvisibleCharacterObjective(Objective):
  """Class representing an Objective which injects invisible characters."""

  def __init__(self, model: GeneratorHubInterface, input: str, ref_translation: str, max_perturbs: int, invisible_chrs: List[str] = [ZWJ,ZWSP,ZWNJ], distance: Callable[[str,str],int] = levenshtein.distance):
    super().__init__(model, input, ref_translation, max_perturbs, distance)
    self.invisible_chrs: List[str] = invisible_chrs

  def bounds(self) -> List[Tuple[float, float]]:
    return [(0,len(self.invisible_chrs)-1), (-1, len(self.input)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]
    for i in range(0, len(perturbations), 2):
      inp_index = integer(perturbations[i+1])
      if inp_index >= 0:
        inv_char = self.invisible_chrs[natural(perturbations[i])]
        candidate = candidate[:inp_index] + [inv_char] + candidate[inp_index:]
    return ''.join(candidate)


class HomoglyphObjective(Objective):

  def __init__(self, model: GeneratorHubInterface, input: str, ref_translation: str, max_perturbs: int, distance: Callable[[str,str],int] = levenshtein.distance, homoglyphs: Dict[str,List[str]] = intentionals):
    super().__init__(model, input, ref_translation, max_perturbs, distance)
    self.homoglyphs = homoglyphs
    self.glyph_map = []
    for i, char in enumerate(self.input):
      if char in self.homoglyphs:
        charmap = self.homoglyphs[char]
        charmap = list(zip([i] * len(charmap), charmap))
        self.glyph_map.extend(charmap)

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1, len(self.glyph_map)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]  
    for perturb in map(integer, perturbations):
      if perturb >= 0:
        i, char = self.glyph_map[perturb]
        candidate[i] = char
    return ''.join(candidate)


class ReorderObjective(Objective):

  def __init__(self, model: GeneratorHubInterface, input: str, ref_translation: str, max_perturbs: int, distance: Callable[[str,str],int] = levenshtein.distance):
    super().__init__(model, input, ref_translation, max_perturbs, distance)

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1,len(self.input)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    def swaps(els) -> str:
      res = ""
      for el in els:
          if isinstance(el, Swap):
              res += swaps([LRO, LRI, RLO, LRI, el.one, PDI, LRI, el.two, PDI, PDF, PDI, PDF])
          elif isinstance(el, str):
              res += el
          else:
              for subel in el:
                  res += swaps([subel])
      return res

    _candidate = [char for char in self.input]
    for perturb in map(integer, perturbations):
      if perturb >= 0 and len(_candidate) >= 2:
        perturb = min(perturb, len(_candidate) - 2)
        _candidate = _candidate[:perturb] + [Swap(_candidate[perturb+1], _candidate[perturb])] + _candidate[perturb+2:]

    return swaps(_candidate)


class DeletionObjective(Objective):
  """Class representing an Objective which injects deletion control characters."""

  def __init__(self, model: GeneratorHubInterface, input: str, ref_translation: str, max_perturbs: int, distance: Callable[[str,str],int] = levenshtein.distance, del_chr: str = BKSP, ins_chr_min: str = '!', ins_chr_max: str = '~'):
    super().__init__(model, input, ref_translation, max_perturbs, distance)
    self.del_chr: str = del_chr
    self.ins_chr_min: str = ins_chr_min
    self.ins_chr_max: str = ins_chr_max

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1,len(self.input)-1), (ord(self.ins_chr_min),ord(self.ins_chr_max))] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]
    for i in range(0, len(perturbations), 2):
      idx = integer(perturbations[i])
      if idx >= 0:
        char = chr(natural(perturbations[i+1]))
        candidate = candidate[:idx] + [char, self.del_chr] + candidate[idx:]
        for j in range(i,len(perturbations), 2):
          perturbations[j] += 2
    return ''.join(candidate)


class MnliObjective():

  def __init__(self, model: GeneratorHubInterface, input: str, hypothesis: str, label:int, max_perturbs: int):
    if not model:
      raise ValueError("Must supply model.")
    if not input:
      raise ValueError("Must supply input.")
    if not hypothesis:
      raise ValueError("Must supply hypothesis.")
    if label == None:
      raise ValueError("Must supply label.")
    self.model: GeneratorHubInterface = model
    self.input: str = input
    self.hypothesis: str = hypothesis
    self.label: int = label
    self.max_perturbs: int = max_perturbs

  def objective(self) -> Callable[[List[float]], float]:
    def _objective(perturbations: List[float]) -> float:
      candidate: str = self.candidate(perturbations)
      tokens = self.model.encode(candidate, self.hypothesis)
      predict = self.model.predict('mnli', tokens)
      if predict.argmax() != self.label:
        return -np.inf
      else:
        return predict.cpu().detach().numpy()[0][self.label]
    return _objective

  def differential_evolution(self, verbose=False, maxiter=3, popsize=32, polish=False) -> str:
    start = process_time()
    result = differential_evolution(self.objective(), self.bounds(),
                                    disp=verbose, maxiter=maxiter,
                                    popsize=popsize, polish=polish)
    end = process_time()
    candidate = self.candidate(result.x)
    tokens = self.model.encode(candidate, self.hypothesis)
    predict = self.model.predict('mnli', tokens)
    probs = softmax(predict, dim=1).cpu().detach().numpy()[0]
    selection = probs.argmax().item()
    inp_tokens = self.model.encode(self.input, self.hypothesis)
    inp_predict = self.model.predict('mnli', inp_tokens)
    inp_probs = softmax(inp_predict, dim=1).cpu().detach().numpy()[0]
    inp_selection = inp_probs.argmax().item()
    return  {
              'adv_example': candidate,
              'adv_example_enc': result.x,
              'input': self.input,
              'hypothesis': self.hypothesis,
              'correct_label_index': self.label,
              'adv_predictions': probs,
              'input_prediction': inp_probs,
              'adv_prediction_correct': selection == self.label,
              'input_prediction_correct': inp_selection == self.label,
              'adv_generation_time': end - start,
              'budget': self.max_perturbs,
              'maxiter': maxiter,
              'popsize': popsize
            }


class InvisibleCharacterMnliObjective(MnliObjective, InvisibleCharacterObjective):
  
  def __init__(self, model: GeneratorHubInterface, input: str, hypothesis: str, label:int, max_perturbs: int, invisible_chrs: List[str] = [ZWJ,ZWSP,ZWNJ]):
    super().__init__(model, input, hypothesis, label, max_perturbs)
    self.invisible_chrs = invisible_chrs


class HomoglyphMnliObjective(MnliObjective, HomoglyphObjective):
  
  def __init__(self, model: GeneratorHubInterface, input: str, hypothesis: str, label:int, max_perturbs: int, homoglyphs: Dict[str,List[str]] = intentionals):
    super().__init__(model, input, hypothesis, label, max_perturbs)
    self.homoglyphs = homoglyphs
    self.glyph_map = []
    for i, char in enumerate(self.input):
      if char in self.homoglyphs:
        charmap = self.homoglyphs[char]
        charmap = list(zip([i] * len(charmap), charmap))
        self.glyph_map.extend(charmap)


class ReorderMnliObjective(MnliObjective, ReorderObjective):
  
  def __init__(self, model: GeneratorHubInterface, input: str, hypothesis: str, label:int, max_perturbs: int):
    super().__init__(model, input, hypothesis, label, max_perturbs)


class DeletionMnliObjective(MnliObjective, DeletionObjective):
  
  def __init__(self, model: GeneratorHubInterface, input: str, hypothesis: str, label:int, max_perturbs: int, del_chr: str = BKSP, ins_chr_min: str = '!', ins_chr_max: str = '~'):
    super().__init__(model, input, hypothesis, label, max_perturbs)
    self.del_chr: str = del_chr
    self.ins_chr_min: str = ins_chr_min
    self.ins_chr_max: str = ins_chr_max


class HomoglyphSpongeObjective(HomoglyphObjective):

  def objective(self) -> Callable[[List[float]], float]:
    def _objective(perturbations: List[float]) -> float:
      candidate: str = self.candidate(perturbations)
      return -1 * timeit(lambda: self.model.translate(candidate), number=1)
    return _objective


class MnliTargetedObjective(MnliObjective):

  def __init__(self, model: GeneratorHubInterface, input: str, hypothesis: str, label: int, target: int, max_perturbs: int):
    super().__init__(model, input, hypothesis, label, max_perturbs)
    self.target = target

  def objective(self) -> Callable[[List[float]], float]:
      def _objective(perturbations: List[float]) -> float:
        candidate: str = self.candidate(perturbations)
        tokens = self.model.encode(candidate, self.hypothesis)
        predict = self.model.predict('mnli', tokens)
        return -softmax(predict, dim=1).cpu().detach().numpy()[0][self.target]
      return _objective

  def differential_evolution(self, verbose=False, maxiter=3, popsize=32, polish=False) -> str:
    start = process_time()
    result = differential_evolution(self.objective(), self.bounds(),
                                    disp=verbose, maxiter=maxiter,
                                    popsize=popsize, polish=polish)
    end = process_time()
    candidate = self.candidate(result.x)
    tokens = self.model.encode(candidate, self.hypothesis)
    predict = self.model.predict('mnli', tokens)
    probs = softmax(predict, dim=1).cpu().detach().numpy()[0]
    selection = probs.argmax().item()
    inp_tokens = self.model.encode(self.input, self.hypothesis)
    inp_predict = self.model.predict('mnli', inp_tokens)
    inp_probs = softmax(inp_predict, dim=1).cpu().detach().numpy()[0]
    inp_selection = inp_probs.argmax().item()
    return {
      'adv_example': candidate,
      'adv_example_enc': result.x,
      'input': self.input,
      'hypothesis': self.hypothesis,
      'golden_label': self.label,
      'adv_predictions': probs,
      'input_prediction': inp_probs,
      'adv_target_success': selection == self.target,
      'adv_golden_correct': selection == self.label,
      'input_golden_correct': inp_selection == self.label,
      'target_label': self.target,
      'adv_selected_label': selection,
      'input_selected_label': inp_selection,
      'adv_generation_time': end - start,
      'budget': self.max_perturbs,
      'maxiter': maxiter,
      'popsize': popsize
    }

class InvisibleCharacterTargetedMnliObjective(MnliTargetedObjective, InvisibleCharacterObjective):
  
  def __init__(self, model: GeneratorHubInterface, input: str, hypothesis: str, label:int, target: int, max_perturbs: int, invisible_chrs: List[str] = [ZWJ,ZWSP,ZWNJ]):
    super().__init__(model, input, hypothesis, label, target, max_perturbs)
    self.invisible_chrs = invisible_chrs


class HomoglyphTargetedMnliObjective(MnliTargetedObjective, HomoglyphObjective):
  
  def __init__(self, model: GeneratorHubInterface, input: str, hypothesis: str, label:int, target: int, max_perturbs: int, homoglyphs: Dict[str,List[str]] = intentionals):
    super().__init__(model, input, hypothesis, label, target, max_perturbs)
    self.homoglyphs = homoglyphs
    self.glyph_map = []
    for i, char in enumerate(self.input):
      if char in self.homoglyphs:
        charmap = self.homoglyphs[char]
        charmap = list(zip([i] * len(charmap), charmap))
        self.glyph_map.extend(charmap)


class ReorderTargetedMnliObjective(MnliTargetedObjective, ReorderObjective):
  
  def __init__(self, model: GeneratorHubInterface, input: str, hypothesis: str, label:int, target: int, max_perturbs: int):
    super().__init__(model, input, hypothesis, label, target, max_perturbs)


class DeletionTargetedMnliObjective(MnliTargetedObjective, DeletionObjective):
  
  def __init__(self, model: GeneratorHubInterface, input: str, hypothesis: str, label:int, target: int, max_perturbs: int, del_chr: str = BKSP, ins_chr_min: str = '!', ins_chr_max: str = '~'):
    super().__init__(model, input, hypothesis, label, target, max_perturbs)
    self.del_chr = del_chr
    self.ins_chr_min: str = ins_chr_min
    self.ins_chr_max: str = ins_chr_max


class MnliTargetedNoLogitsObjective(MnliTargetedObjective):

  def objective(self) -> Callable[[List[float]], float]:
      def _objective(perturbations: List[float]) -> float:
        candidate: str = self.candidate(perturbations)
        tokens = self.model.encode(candidate, self.hypothesis)
        predict = self.model.predict('mnli', tokens)
        if predict.argmax().item() == self.target:
          return -np.inf
        else:
          return np.inf
      return _objective


class InvisibleCharacterTargetedMnliNoLogitsObjective(MnliTargetedNoLogitsObjective, InvisibleCharacterTargetedMnliObjective):
  pass


class HomoglyphTargetedMnliNoLogitsObjective(MnliTargetedNoLogitsObjective, HomoglyphTargetedMnliObjective):
  pass


class ReorderTargetedMnliNoLogitsObjective(MnliTargetedNoLogitsObjective, ReorderTargetedMnliObjective):
  pass


class DeletionTargetedMnliNoLogitsObjective(MnliTargetedNoLogitsObjective, DeletionTargetedMnliObjective):
  pass


class ToxicObjective(ABC):
  """ Toxic content filter objective. """

  def __init__(self, model, input: str, annotated_toxic: bool, max_perturbs: int):
    if not model:
      raise ValueError("Must supply model.")
    if not input:
      raise ValueError("Must supply input.")

    self.model = model
    self.input: str = input
    self.annotated_toxic: bool = annotated_toxic
    self.max_perturbs: int = max_perturbs
    self.output = self.model.predict([self.input])[0]

  def objective(self) -> Callable[[List[float]], float]:
    def _objective(perturbations: List[float]) -> float:
      candidate: str = self.candidate(perturbations)
      predicts: str = self.model.predict([candidate])[0]
      return sum(predicts.values())
    return _objective

  def differential_evolution(self, print_result=True, verbose=True, maxiter=60, popsize=32, polish=False, full_results=False) -> str:
    start = process_time()
    result = differential_evolution(self.objective(), self.bounds(),
                                    disp=verbose, maxiter=maxiter,
                                    popsize=popsize, polish=polish)
    end = process_time()
    candidate = self.candidate(result.x)
    adv_output = self.model.predict([candidate])[0]
    if (print_result):
      print(f"Result: {candidate}")
      print(f"Result Predictions: {adv_output}")
      print(f"Input: {self.input}")
      print(f"Input Predictions: {self.output}")
    if full_results:
      return {
            'adv_example': candidate,
            'adv_example_enc': result.x,
            'input': self.input,
            'annotated_toxic': self.annotated_toxic,
            'adv_predictions': adv_output,
            'input_prediction': self.output,
            'adv_predictions_sum': sum(adv_output.values()),
            'input_prediction_sum': sum(self.output.values()),
            'adv_toxic': adv_output['toxic'] > 0.5,
            'input_toxic': self.output['toxic'] > 0.5,
            'input_annotated_correct': (self.output['toxic'] > 0.5) == self.annotated_toxic,
            'adv_generation_time': end - start,
            'budget': self.max_perturbs,
            'maxiter': maxiter,
            'popsize': popsize
          }
    return candidate

  def bounds(self) -> List[Tuple[float, float]]:
    raise NotImplementedError()

  def candidate(self, perturbations: List[float]) -> str:
    raise NotImplementedError()


class InvisibleToxicObjective(ToxicObjective):
  """Class representing a Toxic Objective which injects invisible characters."""

  def __init__(self, model, input: str, annotated_toxic: bool, max_perturbs: int, invisible_chrs: List[str] = [ZWJ,ZWSP,ZWNJ]):
    super().__init__(model, input, annotated_toxic, max_perturbs)
    self.invisible_chrs: List[str] = invisible_chrs

  def bounds(self) -> List[Tuple[float, float]]:
    return [(0,len(self.invisible_chrs)-1), (-1, len(self.input)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]
    for i in range(0, len(perturbations), 2):
      inp_index = integer(perturbations[i+1])
      if inp_index >= 0:
        inv_char = self.invisible_chrs[natural(perturbations[i])]
        candidate = candidate[:inp_index] + [inv_char] + candidate[inp_index:]
    return ''.join(candidate)


class HomoglyphToxicObjective(ToxicObjective):
  """Class representing a Toxic Objective which injects homoglyphs."""

  def __init__(self, model, input: str, annotated_toxic: bool, max_perturbs: int, homoglyphs: Dict[str,List[str]] = intentionals):
    super().__init__(model, input, annotated_toxic, max_perturbs)
    self.homoglyphs = homoglyphs
    self.glyph_map = []
    for i, char in enumerate(self.input):
      if char in self.homoglyphs:
        charmap = self.homoglyphs[char]
        charmap = list(zip([i] * len(charmap), charmap))
        self.glyph_map.extend(charmap)

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1, len(self.glyph_map)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]  
    for perturb in map(integer, perturbations):
      if perturb >= 0:
        i, char = self.glyph_map[perturb]
        candidate[i] = char
    return ''.join(candidate)


class ReorderToxicObjective(ToxicObjective):
  """Class representing a Toxic Objective which injects homoglyphs."""

  def __init__(self, model, input: str, annotated_toxic: bool, max_perturbs: int):
    super().__init__(model, input, annotated_toxic, max_perturbs)

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1,len(self.input)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    def swaps(els) -> str:
      res = ""
      for el in els:
          if isinstance(el, Swap):
              res += swaps([LRO, LRI, RLO, LRI, el.one, PDI, LRI, el.two, PDI, PDF, PDI, PDF])
          elif isinstance(el, str):
              res += el
          else:
              for subel in el:
                  res += swaps([subel])
      return res

    _candidate = [char for char in self.input]
    for perturb in map(integer, perturbations):
      if perturb >= 0 and len(_candidate) >= 2:
        perturb = min(perturb, len(_candidate) - 2)
        _candidate = _candidate[:perturb] + [Swap(_candidate[perturb+1], _candidate[perturb])] + _candidate[perturb+2:]

    return swaps(_candidate)


class DeletionToxicObjective(ToxicObjective):
  """Class representing a Toxic Objective which injects homoglyphs."""

  def __init__(self, model, input: str, annotated_toxic: bool, max_perturbs: int, del_chr: str = BKSP, ins_chr_min: str = '!', ins_chr_max: str = '~'):
    super().__init__(model, input, annotated_toxic, max_perturbs)
    self.del_chr = del_chr
    self.ins_chr_min: str = ins_chr_min
    self.ins_chr_max: str = ins_chr_max

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1,len(self.input)-1), (ord(self.ins_chr_min),ord(self.ins_chr_max))] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]
    for i in range(0, len(perturbations), 2):
      idx = integer(perturbations[i])
      if idx >= 0:
        char = chr(natural(perturbations[i+1]))
        candidate = candidate[:idx] + [char, self.del_chr] + candidate[idx:]
        for j in range(i,len(perturbations), 2):
          perturbations[j] += 2
    return ''.join(candidate)


class ToxicPerspectiveObjective(ABC):
  """Class representing attacks against Google's toxic language Perspective API."""

  def __init__(self, client, input: str, rate_limit: int, max_perturbs: int, annotated_toxic: bool):
    if not client:
      raise ValueError("Must supply client.")
    if not input:
      raise ValueError("Must supply input.")

    self.client = client
    self.input: str = input
    # Perspective API rate limit in seconds
    self.rate_limit: float = 1 / rate_limit + .005
    self.max_perturbs: int = max_perturbs
    self.annotated_toxic = annotated_toxic
    self.last_request = time()
    self.output = self.request(input)

  def request(self, candidate: str):
    delta = time() - self.last_request
    if delta < self.rate_limit:
      sleep(self.rate_limit - delta)
    self.last_request = time()
    analyze_request = {
        'comment': { 'text': candidate },
        'requestedAttributes': {'TOXICITY': {}},
        'languages': 'en',
      }
    response = self.client.comments().analyze(body=analyze_request).execute()
    return response['attributeScores']['TOXICITY']['summaryScore']['value']

  def objective(self) -> Callable[[List[float]], float]:
    def _objective(perturbations: List[float]) -> float:
      candidate: str = self.candidate(perturbations)
      return self.request(candidate)
    return _objective
    
  def differential_evolution(self, print_result=True, verbose=True, maxiter=60, popsize=32, polish=False, full_results=False) -> str:
    start = process_time()
    result = differential_evolution(self.objective(), self.bounds(),
                                    disp=verbose, maxiter=maxiter,
                                    popsize=popsize, polish=polish)
    end = process_time()
    candidate = self.candidate(result.x)
    adv_output = self.request(candidate)
    if (print_result):
      print(f"Result: {candidate}")
      print(f"Result Predictions: {adv_output}")
      print(f"Input: {self.input}")
      print(f"Input Predictions: {self.output}")
    if full_results:
      return {
            'adv_example': candidate,
            'adv_example_enc': result.x,
            'input': self.input,
            'annotated_toxic': self.annotated_toxic,
            'adv_predictions': adv_output,
            'input_prediction': self.output,
            'adv_toxic': adv_output > 0.5,
            'input_toxic': self.output > 0.5,
            'input_annotated_correct': (self.output > 0.5) == self.annotated_toxic,
            'adv_generation_time': end - start,
            'budget': self.max_perturbs,
            'maxiter': maxiter,
            'popsize': popsize
          }
    return candidate

  def bounds(self) -> List[Tuple[float, float]]:
    raise NotImplementedError()

  def candidate(self, perturbations: List[float]) -> str:
    raise NotImplementedError()


class InvisibleToxicPerspectiveObjective(ToxicPerspectiveObjective):
  """Class representing a Toxic Perspective API Objective which injects invisible characters."""

  def __init__(self, client, input: str, rate_limit: int, annotated_toxic: bool, max_perturbs: int, invisible_chrs: List[str] = [ZWJ,ZWSP,ZWNJ]):
    super().__init__(client, input, rate_limit, max_perturbs, annotated_toxic)
    self.invisible_chrs: List[str] = invisible_chrs

  def bounds(self) -> List[Tuple[float, float]]:
    return [(0,len(self.invisible_chrs)-1), (-1, len(self.input)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]
    for i in range(0, len(perturbations), 2):
      inp_index = integer(perturbations[i+1])
      if inp_index >= 0:
        inv_char = self.invisible_chrs[natural(perturbations[i])]
        candidate = candidate[:inp_index] + [inv_char] + candidate[inp_index:]
    return ''.join(candidate)


class HomoglyphToxicPerspectiveObjective(ToxicPerspectiveObjective):
  """Class representing a Toxic Perspective API Objective which injects homoglyphs."""

  def __init__(self, client, input: str, rate_limit: int, annotated_toxic: bool, max_perturbs: int, homoglyphs: Dict[str,List[str]] = intentionals):
    super().__init__(client, input, rate_limit, max_perturbs, annotated_toxic)
    self.homoglyphs = homoglyphs
    self.glyph_map = []
    for i, char in enumerate(self.input):
      if char in self.homoglyphs:
        charmap = self.homoglyphs[char]
        charmap = list(zip([i] * len(charmap), charmap))
        self.glyph_map.extend(charmap)

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1, len(self.glyph_map)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]  
    for perturb in map(integer, perturbations):
      if perturb >= 0:
        i, char = self.glyph_map[perturb]
        candidate[i] = char
    return ''.join(candidate)


class ReorderToxicPerspectiveObjective(ToxicPerspectiveObjective):
  """Class representing a Toxic Perspective API Objective which injects reorderings."""

  def __init__(self, client, input: str, rate_limit: int, annotated_toxic: bool, max_perturbs: int):
    super().__init__(client, input, rate_limit, max_perturbs, annotated_toxic)

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1,len(self.input)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    def swaps(els) -> str:
      res = ""
      for el in els:
          if isinstance(el, Swap):
              res += swaps([LRO, LRI, RLO, LRI, el.one, PDI, LRI, el.two, PDI, PDF, PDI, PDF])
          elif isinstance(el, str):
              res += el
          else:
              for subel in el:
                  res += swaps([subel])
      return res

    _candidate = [char for char in self.input]
    for perturb in map(integer, perturbations):
      if perturb >= 0 and len(_candidate) >= 2:
        perturb = min(perturb, len(_candidate) - 2)
        _candidate = _candidate[:perturb] + [Swap(_candidate[perturb+1], _candidate[perturb])] + _candidate[perturb+2:]

    return swaps(_candidate)


class DeletionToxicPerspectiveObjective(ToxicPerspectiveObjective):
  """Class representing a Toxic Perspective API Objective which injects deletions."""

  def __init__(self, client, input: str, rate_limit: int, annotated_toxic: bool, max_perturbs: int, del_chr: str = BKSP, ins_chr_min: str = '!', ins_chr_max: str = '~'):
    super().__init__(client, input, rate_limit, max_perturbs, annotated_toxic)
    self.del_chr = del_chr
    self.ins_chr_min: str = ins_chr_min
    self.ins_chr_max: str = ins_chr_max

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1,len(self.input)-1), (ord(self.ins_chr_min),ord(self.ins_chr_max))] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]
    for i in range(0, len(perturbations), 2):
      idx = integer(perturbations[i])
      if idx >= 0:
        char = chr(natural(perturbations[i+1]))
        candidate = candidate[:idx] + [char, self.del_chr] + candidate[idx:]
        for j in range(i,len(perturbations), 2):
          perturbations[j] += 2
    return ''.join(candidate)


class MemoryCache(Cache):
    _CACHE = {}

    def get(self, url):
        return MemoryCache._CACHE.get(url)

    def set(self, url, content):
        MemoryCache._CACHE[url] = content


class SpongeObjective(ABC):
  """Class representing availability (sponge) translation attacks."""

  def objective(self) -> Callable[[List[float]], float]:
    def _objective(perturbations: List[float]) -> float:
      candidate: str = self.candidate(perturbations)
      return -1 * timeit(lambda: self.model.translate(candidate), number=1)
    return _objective

  def differential_evolution(self, verbose=False, maxiter=60, popsize=32, polish=False) -> str:
    start = process_time()
    result = differential_evolution(self.objective(), self.bounds(),
                                    disp=verbose, maxiter=maxiter,
                                    popsize=popsize, polish=polish)
    end = process_time()
    candidate = self.candidate(result.x)
    input_inf_time = timeit(lambda: self.model.translate(self.input), number=1)
    return  {
              'adv_example': candidate,
              'adv_example_enc': result.x,
              'input_translation_distance': self.distance(candidate, self.input),
              'ref_translation_distance': self.distance(candidate, self.ref_translation),
              'input': self.input,
              'input_translation': self.output,
              'adv_translation': self.model.translate(candidate),
              'ref_translation': self.ref_translation,
              'input_inference_time': input_inf_time,
              'adv_inference_time': -result.fun,
              'ref_bleu': corpus_bleu(candidate, self.ref_translation).score,
              'input_bleu': corpus_bleu(candidate, self.input).score,
              'adv_generation_time': end - start,
              'budget': self.max_perturbs,
              'maxiter': maxiter,
              'popsize': popsize
            }


class InvisibleCharacterSpongeObjective(SpongeObjective, InvisibleCharacterObjective):
  pass


class HomoglyphSpongeObjective(SpongeObjective, HomoglyphObjective):
  pass


class ReorderSpongeObjective(SpongeObjective, ReorderObjective):
  pass


class DeletionSpongeObjective(SpongeObjective, DeletionObjective):
  pass


class NerTargetedObjective():

  def __init__(self, model: Pipeline, input: str, labels: List[str], target: str, max_perturbs: int):
    self.model: Pipeline = model
    self.input: str = input
    self.labels: List[str] = labels
    self.max_perturbs: int = max_perturbs
    self.target = target

  def objective(self) -> Callable[[List[float]], float]:
      def _objective(perturbations: List[float]) -> float:
        candidate: str = defence(self.candidate(perturbations))
        predicts = self.model(candidate)
        score = 0
        for predict in predicts:
          if predict['entity'].endswith(self.target):
            score += predict['score']
        return -score
      return _objective

  def differential_evolution(self, verbose=False, maxiter=3, popsize=32, polish=False) -> str:
    start = process_time()
    result = differential_evolution(self.objective(), self.bounds(),
                                    disp=verbose, maxiter=maxiter,
                                    popsize=popsize, polish=polish)
    end = process_time()
    successful_attack = result.fun < 0
    candidate = self.candidate(result.x)
    predicts = self.model(candidate)
    inp_predicts = self.model(self.input)
    return {
      'adv_example': candidate,
      'adv_example_enc': result.x,
      'input': self.input,
      'golden_labels': self.labels,
      'adv_predictions': predicts,
      'input_prediction': inp_predicts,
      'adv_target_success': successful_attack,
      'target_label': self.target,
      'adv_generation_time': end - start,
      'budget': self.max_perturbs,
      'maxiter': maxiter,
      'popsize': popsize
    }


class InvisibleCharacterNerTargetedObjective(NerTargetedObjective):
  
  def __init__(self, model: Pipeline, input: str, labels: List[str], target: str, max_perturbs: int, invisible_chrs: List[str] = [ZWJ,ZWSP,ZWNJ]):
    super().__init__(model, input, labels, target, max_perturbs)
    self.invisible_chrs = invisible_chrs

  def bounds(self) -> List[Tuple[float, float]]:
    return [(0,len(self.invisible_chrs)-1), (-1, len(self.input)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]
    for i in range(0, len(perturbations), 2):
      inp_index = integer(perturbations[i+1])
      if inp_index >= 0:
        inv_char = self.invisible_chrs[natural(perturbations[i])]
        candidate = candidate[:inp_index] + [inv_char] + candidate[inp_index:]
    return ''.join(candidate)


class HomoglyphNerTargetedObjective(NerTargetedObjective):

  def __init__(self, model: Pipeline, input: str, labels: List[str], target: str, max_perturbs: int, homoglyphs: Dict[str,List[str]] = intentionals):
    super().__init__(model, input, labels, target, max_perturbs)
    self.homoglyphs = homoglyphs
    self.glyph_map = []
    for i, char in enumerate(self.input):
      if char in self.homoglyphs:
        charmap = self.homoglyphs[char]
        charmap = list(zip([i] * len(charmap), charmap))
        self.glyph_map.extend(charmap)

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1, len(self.glyph_map)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]  
    for perturb in map(integer, perturbations):
      if perturb >= 0:
        i, char = self.glyph_map[perturb]
        candidate[i] = char
    return ''.join(candidate)

class ReorderNerTargetedObjective(NerTargetedObjective):

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1,len(self.input)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    def swaps(els) -> str:
      res = ""
      for el in els:
          if isinstance(el, Swap):
              res += swaps([LRO, LRI, RLO, LRI, el.one, PDI, LRI, el.two, PDI, PDF, PDI, PDF])
          elif isinstance(el, str):
              res += el
          else:
              for subel in el:
                  res += swaps([subel])
      return res

    _candidate = [char for char in self.input]
    for perturb in map(integer, perturbations):
      if perturb >= 0 and len(_candidate) >= 2:
        perturb = min(perturb, len(_candidate) - 2)
        _candidate = _candidate[:perturb] + [Swap(_candidate[perturb+1], _candidate[perturb])] + _candidate[perturb+2:]

    return swaps(_candidate)


class DeletionNerTargetedObjective(NerTargetedObjective):

  def __init__(self, model: Pipeline, input: str, labels: List[str], target: str, max_perturbs: int, del_chr: str = BKSP, ins_chr_min: str = '!', ins_chr_max: str = '~'):
    super().__init__(model, input, labels, target, max_perturbs)
    self.del_chr: str = del_chr
    self.ins_chr_min: str = ins_chr_min
    self.ins_chr_max: str = ins_chr_max

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1,len(self.input)-1), (ord(self.ins_chr_min),ord(self.ins_chr_max))] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]
    for i in range(0, len(perturbations), 2):
      idx = integer(perturbations[i])
      char = chr(natural(perturbations[i+1]))
      candidate = candidate[:idx] + [char, self.del_chr] + candidate[idx:]
      for j in range(i,len(perturbations), 2):
        perturbations[j] += 2
    return ''.join(candidate)


class EmotionTargetedObjective():

  def __init__(self, model: Pipeline, input: str, label: str, target: int, max_perturbs: int):
    self.model: Pipeline = model
    self.input: str = input
    self.label: str = label
    self.max_perturbs: int = max_perturbs
    self.target: int = target

  def objective(self) -> Callable[[List[float]], float]:
      def _objective(perturbations: List[float]) -> float:
        candidate: str = self.candidate(perturbations)
        predicts = self.model(candidate)[0]
        score = predicts[self.target]['score']
        if np.argmax(list(map(lambda x: x['score'], predicts))) == self.target:
          score += 1
        return -score
      return _objective

  def differential_evolution(self, verbose=False, maxiter=3, popsize=32, polish=False) -> str:
    start = process_time()
    result = differential_evolution(self.objective(), self.bounds(),
                                    disp=verbose, maxiter=maxiter,
                                    popsize=popsize, polish=polish)
    end = process_time()
    successful_attack = result.fun <= -1
    candidate = self.candidate(result.x)
    predicts = self.model(candidate)
    inp_predicts = self.model(self.input)
    return {
      'adv_example': candidate,
      'adv_example_enc': result.x,
      'input': self.input,
      'golden_label': self.label,
      'adv_predictions': predicts,
      'input_prediction': inp_predicts,
      'adv_target_success': successful_attack,
      'target_label': emotion_classes[self.target],
      'target_index': self.target,
      'adv_generation_time': end - start,
      'budget': self.max_perturbs,
      'maxiter': maxiter,
      'popsize': popsize
    }


class InvisibleCharacterEmotionTargetedObjective(EmotionTargetedObjective):
  
  def __init__(self, model: Pipeline, input: str, labels: List[str], target: str, max_perturbs: int, invisible_chrs: List[str] = [ZWJ,ZWSP,ZWNJ]):
    super().__init__(model, input, labels, target, max_perturbs)
    self.invisible_chrs = invisible_chrs

  def bounds(self) -> List[Tuple[float, float]]:
    return [(0,len(self.invisible_chrs)-1), (-1, len(self.input)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]
    for i in range(0, len(perturbations), 2):
      inp_index = integer(perturbations[i+1])
      if inp_index >= 0:
        inv_char = self.invisible_chrs[natural(perturbations[i])]
        candidate = candidate[:inp_index] + [inv_char] + candidate[inp_index:]
    return ''.join(candidate)


class HomoglyphEmotionTargetedObjective(EmotionTargetedObjective):

  def __init__(self, model: Pipeline, input: str, labels: List[str], target: str, max_perturbs: int, homoglyphs: Dict[str,List[str]] = intentionals):
    super().__init__(model, input, labels, target, max_perturbs)
    self.homoglyphs = homoglyphs
    self.glyph_map = []
    for i, char in enumerate(self.input):
      if char in self.homoglyphs:
        charmap = self.homoglyphs[char]
        charmap = list(zip([i] * len(charmap), charmap))
        self.glyph_map.extend(charmap)

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1, len(self.glyph_map)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]  
    for perturb in map(integer, perturbations):
      if perturb >= 0:
        i, char = self.glyph_map[perturb]
        candidate[i] = char
    return ''.join(candidate)

class ReorderEmotionTargetedObjective(EmotionTargetedObjective):

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1,len(self.input)-1)] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    def swaps(els) -> str:
      res = ""
      for el in els:
          if isinstance(el, Swap):
              res += swaps([LRO, LRI, RLO, LRI, el.one, PDI, LRI, el.two, PDI, PDF, PDI, PDF])
          elif isinstance(el, str):
              res += el
          else:
              for subel in el:
                  res += swaps([subel])
      return res

    _candidate = [char for char in self.input]
    for perturb in map(integer, perturbations):
      if perturb >= 0 and len(_candidate) >= 2:
        perturb = min(perturb, len(_candidate) - 2)
        _candidate = _candidate[:perturb] + [Swap(_candidate[perturb+1], _candidate[perturb])] + _candidate[perturb+2:]

    return swaps(_candidate)


class DeletionEmotionTargetedObjective(EmotionTargetedObjective):

  def __init__(self, model: Pipeline, input: str, labels: List[str], target: str, max_perturbs: int, del_chr: str = BKSP, ins_chr_min: str = '!', ins_chr_max: str = '~'):
    super().__init__(model, input, labels, target, max_perturbs)
    self.del_chr: str = del_chr
    self.ins_chr_min: str = ins_chr_min
    self.ins_chr_max: str = ins_chr_max

  def bounds(self) -> List[Tuple[float, float]]:
    return [(-1,len(self.input)-1), (ord(self.ins_chr_min),ord(self.ins_chr_max))] * self.max_perturbs

  def candidate(self, perturbations: List[float]) -> str:
    candidate = [char for char in self.input]
    for i in range(0, len(perturbations), 2):
      idx = integer(perturbations[i])
      char = chr(natural(perturbations[i+1]))
      candidate = candidate[:idx] + [char, self.del_chr] + candidate[idx:]
      for j in range(i,len(perturbations), 2):
        perturbations[j] += 2
    return ''.join(candidate)


# --- Helper Functions ---

def some(*els):
    """Returns the arguments as a tuple with Nones removed."""
    return tuple(filter(None, tuple(els)))

def swaps(chars: str) -> set:
    """Generates all possible swaps for a string."""
    def pairs(chars, pre=(), suf=()):
        orders = set()
        for i in range(len(chars)-1):
            prefix = pre + tuple(chars[:i])
            suffix = suf + tuple(chars[i+2:])
            swap = Swap(chars[i+1], chars[i])
            pair = some(prefix, swap, suffix)
            orders.add(pair)
            orders.update(pairs(suffix, pre=some(prefix, swap)))
            orders.update(pairs(some(prefix, swap), suf=suffix))
        return orders
    return pairs(chars) | {tuple(chars)}

def unswap(el: tuple) -> str:
    """Reverts a tuple of swaps to the original string."""
    if isinstance(el, str):
        return el
    elif isinstance(el, Swap):
        return unswap((el.two, el.one))
    else:
        res = ""
        for e in el:
            res += unswap(e)
        return res

def uniswap(els):
    res = ""
    for el in els:
        if isinstance(el, Swap):
            res += uniswap([LRO, LRI, RLO, LRI, el.one, PDI, LRI, el.two, PDI, PDF, PDI, PDF])
        elif isinstance(el, str):
            res += el
        else:
            for subel in el:
                res += uniswap([subel])
    return res

def natural(x: float) -> int:
    """Rounds float to the nearest natural number (positive int)"""
    return max(0, round(float(x)))

def integer(x: float) -> int:
    """Rounds float to the nearest int"""
    return round(float(x))

def detokenize(tokens: List[str]) -> str:
  output = ""
  for index, token in enumerate(tokens):
    if (len(token) == 1 and token in punctuation) or index == 0:
      output += token
    else:
      output += ' ' + token
  return output

def ner_tags(tags: List[int]) -> List[str]:
  return list(map(lambda x: ner_labels[x], tags))

def load_source(num_examples):
  # Build source and target mappings for BLEU scoring
  source = dict()
  target = dict()
  with open('newstest2014-fren-src.en.sgm', 'r') as f:
    source_doc = BeautifulSoup(f, 'html.parser')
  with open('newstest2014-fren-ref.fr.sgm', 'r') as f:
    target_doc = BeautifulSoup(f, 'html.parser')
  for doc in source_doc.find_all('doc'):
    source[str(doc['docid'])] = dict()
    for seg in doc.find_all('seg'):
      source[str(doc['docid'])][str(seg['id'])] = str(seg.string)
  for docid, doc in source.items():
    target[docid] = dict()
    for segid in doc:
      node = target_doc.select_one(f'doc[docid="{docid}"] > seg[id="{segid}"]')
      target[docid][segid] = str(node.string)
  # Sort the examples in order of length to improve runtime
  source_list = []
  target_list = []
  for docid, doc in source.items():
    for segid, seg in doc.items():
      source_list.append({ docid: { segid: seg }})
  source_list.sort(key=lambda x: len(str(list(list(x.values())[0].values())[0])))
  source_list = source_list[:num_examples]
  for example in source_list:
    for docid, doc in example.items():
      for segid, seg in doc.items():
        target_list.append({ docid: { segid: target[docid][segid] }})
  return source_list, target_list, len(source_list)

def load_ner_data(num_exmaples):
  return load_dataset("conll2003", split=f'test[:{num_exmaples}]')

def load_emotion_data(num_exmaples):
  return load_dataset("emotion", split=f'test[:{num_exmaples}]')

def experiment(model, objective, source, target, min_perturb, max_perturb, file, maxiter, popsize, n_examples, label, overwrite):
  if overwrite or not exists(file):
    perturbs = { label: { '0': dict() } }
  else:
    with open(file, 'rb') as f:
      perturbs = pickle.load(f)
    if label not in perturbs:
      perturbs[label] = dict()
    if '0' not in perturbs[label]:
      perturbs[label]['0'] = dict()
  for i, example in enumerate(source):
    for docid, doc in example.items():
      if docid not in perturbs[label]['0']:
        perturbs[label]['0'][docid] = dict()
      for segid, seg in doc.items():
        if segid not in perturbs[label]['0'][docid]:
          ref = target[i][docid][segid]
          output = model.translate(seg)
          perturbs[label]['0'][docid][segid] = {
                'adv_example': seg,
                'adv_example_enc': [],
                'adv_translation': output,
                'input_translation_distance': levenshtein.distance(output, output),
                'ref_translation_distance': levenshtein.distance(output, ref),
                'input': seg,
                'input_translation': output,
                'adv_translation': output,
                'ref_translation': ref,
                'ref_bleu': corpus_bleu(output, ref).score,
                'input_bleu': corpus_bleu(output, output).score,
                'adv_generation_time': 0,
                'budget': 0,
                'maxiter': maxiter,
                'popsize': popsize
              }
  with tqdm(total=n_examples*(max_perturb-min_perturb+1), desc="Adv. Examples") as pbar:
    for i in range(min_perturb, max_perturb+1):
      if str(i) not in perturbs[label]:
        perturbs[label][str(i)] = dict()
      for j, example in enumerate(source):
        for docid, doc in example.items():
          if docid not in perturbs[label][str(i)]:
            perturbs[label][str(i)][docid] = dict()
          for segid, seg in doc.items():
            if segid not in perturbs[label][str(i)][docid]:
              ref = target[j][docid][segid]
              perturbs[label][str(i)][docid][segid] = objective(en2fr, seg, ref, i).differential_evolution(maxiter=maxiter, popsize=popsize)
              with open(file, 'wb') as f:
                pickle.dump(perturbs, f)
            else:
              # Required for progress bar to update correctly
              sleep(0.1)
            pbar.update(1)

def mnli_experiment(model, objective, data, file, min_budget, max_budget, maxiter, popsize, exp_label, overwrite):
  if overwrite or not exists(file):
    perturbs = { exp_label: { '0': dict() } }
  else:
    with open(file, 'rb') as f:
      perturbs = pickle.load(f)
    if exp_label not in perturbs:
      perturbs[exp_label] = dict()
    if '0' not in perturbs[exp_label]:
      perturbs[exp_label]['0'] = dict()
  for test in data:
    if test['pairID'] not in perturbs[exp_label]['0']:
      tokens = model.encode(test['sentence1'], test['sentence2'])
      predict = model.predict('mnli', tokens)
      probs = softmax(predict, dim=1).cpu().detach().numpy()[0]
      label = label_map[test['gold_label']]
      correct = probs.argmax().item() == label
      perturbs[exp_label]['0'][test['pairID']] = {
          'adv_example': test['sentence1'],
          'adv_example_enc': [],
          'input': test['sentence1'],
          'hypothesis': test['sentence2'],
          'correct_label_index': label,
          'adv_predictions': probs,
          'input_prediction': probs,
          'adv_prediction_correct': correct,
          'input_prediction_correct': correct,
          'adv_generation_time': 0,
          'budget': 0,
          'maxiter': maxiter,
          'popsize': popsize
        }
  with tqdm(total=len(data)*(max_budget-min_budget+1), desc="Adv. Examples") as pbar:
    for budget in range(min_budget, max_budget+1):
      if str(budget) not in perturbs[exp_label]:
        perturbs[exp_label][str(budget)] = dict()
      for test in data:
        if test['pairID'] not in perturbs[exp_label][str(budget)]:
          obj = objective(mnli, test['sentence1'], test['sentence2'], label_map[test['gold_label']], budget)
          example = obj.differential_evolution(maxiter=maxiter, popsize=popsize)
          perturbs[exp_label][str(budget)][test['pairID']] = example
          with open(file, 'wb') as f:
            pickle.dump(perturbs, f)
        else:
          # Required for progress bar to update correctly
          sleep(0.1)
        pbar.update(1)


def mnli_targeted_experiment(objective, model, inputs, file, min_budget, max_budget, maxiter, popsize, exp_label, overwrite):
  if overwrite or not exists(file):
    perturbs = { exp_label: { '0': dict() } }
  else:
    with open(file, 'rb') as f:
      perturbs = pickle.load(f)
    if exp_label not in perturbs:
      perturbs[exp_label] = dict()
    if '0' not in perturbs[exp_label]:
      perturbs[exp_label]['0'] = dict()
  for test in inputs:
    tokens = model.encode(test['sentence1'], test['sentence2'])
    predict = model.predict('mnli', tokens)
    probs = softmax(predict, dim=1).cpu().detach().numpy()[0]
    selection = probs.argmax().item()
    label = label_map[test['gold_label']]
    correct = predict.argmax().item() == label
    if test['pairID'] not in perturbs[exp_label]['0']:
      perturbs[exp_label]['0'][test['pairID']] = dict()
    for target in range(len(label_map)):
      if str(target) not in perturbs[exp_label]['0'][test['pairID']]:
        perturbs[exp_label]['0'][test['pairID']][str(target)] = {
              'adv_example': test['sentence1'],
              'adv_example_enc': [],
              'input': test['sentence1'],
              'hypothesis': test['sentence2'],
              'golden_label': label,
              'adv_predictions': probs,
              'input_prediction': probs,
              'adv_target_success': selection == target,
              'adv_golden_correct': selection == label,
              'input_golden_correct': selection == label,
              'target_label': target,
              'adv_selected_label': selection,
              'input_selected_label': selection,
              'adv_generation_time': 0,
              'budget': 0,
              'maxiter': maxiter,
              'popsize': popsize
            }
  with tqdm(total=len(inputs)*(max_budget-min_budget+1)*len(label_map), desc="Adv. Examples") as pbar:
    for budget in range(min_budget, max_budget+1):
      if str(budget) not in perturbs[exp_label]:
        perturbs[exp_label][str(budget)] = dict()
      for input in inputs:
        if input['pairID'] not in perturbs[exp_label][str(budget)]:
          perturbs[exp_label][str(budget)][input['pairID']] = dict()
        for target in range(len(label_map)):
          if str(target) not in perturbs[exp_label][str(budget)][input['pairID']]:
            obj = objective(model, input['sentence1'], input['sentence2'], label_map[input['gold_label']], target, budget)
            example = obj.differential_evolution(verbose=False, maxiter=maxiter, popsize=popsize)
            perturbs[exp_label][str(budget)][input['pairID']][str(target)] = example
            with open(file, 'wb') as f:
              pickle.dump(perturbs, f)
          else:
            # Required for progress bar to update correctly
            sleep(0.1)
          pbar.update(1)

def max_toxic_experiment(objective, model, file, min_budget, max_budget, examples, maxiter, popsize, exp_label, overwrite):
  if overwrite or not exists(file):
    perturbs = { exp_label: { '0': dict() } }
  else:
    with open(file, 'rb') as f:
      perturbs = pickle.load(f)
    if exp_label not in perturbs:
      perturbs[exp_label] = dict()
    if '0' not in perturbs[exp_label]:
      perturbs[exp_label]['0'] = dict()
  for example in examples:
    if str(example['rev_id']) not in perturbs[exp_label]['0']:
      probs = model.predict([example['comment']])[0]
      perturbs[exp_label]['0'][str(example['rev_id'])] = {
              'adv_example': example['comment'],
              'adv_example_enc': [],
              'input': example['comment'],
              'annotated_toxic': example['toxicity'],
              'adv_predictions': probs,
              'input_prediction': probs,
              'adv_predictions_sum': sum(probs.values()),
              'input_prediction_sum': sum(probs.values()),
              'adv_toxic': probs['toxic'] > 0.5,
              'input_toxic': probs['toxic'] > 0.5,
              'input_annotated_correct': (probs['toxic'] > 0.5) == example['toxicity'],
              'adv_generation_time': 0,
              'budget': 0,
              'maxiter': maxiter,
              'popsize': popsize
            }
  with tqdm(total=len(examples)*(max_budget-min_budget+1), desc="Adv. Examples") as pbar:
    for budget in range(min_budget, max_budget+1):
      if str(budget) not in perturbs[exp_label]:
        perturbs[exp_label][str(budget)] = dict()
      for example in examples:
        if str(example['rev_id']) not in perturbs[exp_label][str(budget)]:
          obj = objective(model, example['comment'], example['toxicity'], budget)
          result = obj.differential_evolution(verbose=False, print_result=False, maxiter=maxiter, popsize=popsize, full_results=True)
          perturbs[exp_label][str(budget)][str(example['rev_id'])] = result
          with open(file, 'wb') as f:
            pickle.dump(perturbs, f)
        else:
          # Required for progress bar to update correctly
          sleep(0.1)
        pbar.update(1)

def perspective_experiment(objective, client, file, min_budget, max_budget, examples, maxiter, popsize, exp_label, overwrite, rate_limit):
  if overwrite or not exists(file):
    perturbs = { exp_label: { '0': dict() } }
  else:
    with open(file, 'rb') as f:
      perturbs = pickle.load(f)
    if exp_label not in perturbs:
      perturbs[exp_label] = dict()
    if '0' not in perturbs[exp_label]:
      perturbs[exp_label]['0'] = dict()
  obj = objective(client, "zero", rate_limit, False, 0)
  for example in examples:
    if str(example['rev_id']) not in perturbs[exp_label]['0']:
      probs = obj.request(example['comment'])
      perturbs[exp_label]['0'][str(example['rev_id'])] = {
              'adv_example': example['comment'],
              'adv_example_enc': [],
              'input': example['comment'],
              'annotated_toxic': example['toxicity'],
              'adv_predictions': probs,
              'input_prediction': probs,
              'adv_toxic': probs > 0.5,
              'input_toxic': probs > 0.5,
              'input_annotated_correct': (probs > 0.5) == example['toxicity'],
              'adv_generation_time': 0,
              'budget': 0,
              'maxiter': maxiter,
              'popsize': popsize
            }
  with tqdm(total=len(examples)*(max_budget-min_budget+1), desc="Adv. Examples") as pbar:
    for budget in range(min_budget, max_budget+1):
      if str(budget) not in perturbs[exp_label]:
        perturbs[exp_label][str(budget)] = dict()
      for example in examples:
        if str(example['rev_id']) not in perturbs[exp_label][str(budget)]:
          obj = objective(client, example['comment'], rate_limit, example['toxicity'], budget)
          result = obj.differential_evolution(verbose=False, print_result=False, maxiter=maxiter, popsize=popsize, full_results=True)
          perturbs[exp_label][str(budget)][str(example['rev_id'])] = result
          with open(file, 'wb') as f:
            pickle.dump(perturbs, f)
        else:
          # Required for progress bar to update correctly
          sleep(0.1)
        pbar.update(1)

def sponge_experiment(model, objective, source, target, min_perturb, max_perturb, file, maxiter, popsize, n_examples, label, overwrite):
  if overwrite or not exists(file):
    perturbs = { label: { '0': dict() } }
  else:
    with open(file, 'rb') as f:
      perturbs = pickle.load(f)
    if label not in perturbs:
      perturbs[label] = dict()
    if '0' not in perturbs[label]:
      perturbs[label]['0'] = dict()
  for i, example in enumerate(source):
    for docid, doc in example.items():
      if docid not in perturbs[label]['0']:
        perturbs[label]['0'][docid] = dict()
      for segid, seg in doc.items():
        if segid not in perturbs[label]['0'][docid]:
          ref = target[i][docid][segid]
          output = model.translate(seg)
          inf_time = timeit(lambda: model.translate(seg), number=1)
          perturbs[label]['0'][docid][segid] = {
                'adv_example': seg,
                'adv_example_enc': [],
                'input_translation_distance': levenshtein.distance(seg, seg),
                'ref_translation_distance': levenshtein.distance(seg, ref),
                'input': seg,
                'input_translation': output,
                'adv_translation': output,
                'ref_translation': ref,
                'input_inference_time': inf_time,
                'adv_inference_time': inf_time,
                'ref_bleu': corpus_bleu(seg, ref).score,
                'input_bleu': corpus_bleu(seg, seg).score,
                'adv_generation_time': 0,
                'budget': 0,
                'maxiter': maxiter,
                'popsize': popsize
              }
  with tqdm(total=n_examples*(max_perturb-min_perturb+1), desc="Adv. Examples") as pbar:
    for i in range(min_perturb, max_perturb+1):
      if str(i) not in perturbs[label]:
        perturbs[label][str(i)] = dict()
      for j, example in enumerate(source):
        for docid, doc in example.items():
          if docid not in perturbs[label][str(i)]:
            perturbs[label][str(i)][docid] = dict()
          for segid, seg in doc.items():
            if segid not in perturbs[label][str(i)][docid]:
              ref = target[j][docid][segid]
              perturbs[label][str(i)][docid][segid] = objective(en2fr, seg, ref, i).differential_evolution(maxiter=maxiter, popsize=popsize)
              with open(file, 'wb') as f:
                pickle.dump(perturbs, f)
            else:
              # Required for progress bar to update correctly
              sleep(0.1)
            pbar.update(1)

def ner_targeted_experiment(objective, model, inputs, file, min_budget, max_budget, maxiter, popsize, exp_label, overwrite):
  if overwrite or not exists(file):
    perturbs = { exp_label: { '0': dict() } }
  else:
    with open(file, 'rb') as f:
      perturbs = pickle.load(f)
    if exp_label not in perturbs:
      perturbs[exp_label] = dict()
    if '0' not in perturbs[exp_label]:
      perturbs[exp_label]['0'] = dict()
  for test in inputs:
    input = detokenize(test['tokens'])
    predicts = model(input)
    labels = ner_tags(test['ner_tags'])
    if test['id'] not in perturbs[exp_label]['0']:
      perturbs[exp_label]['0'][test['id']] = dict()
    for target in ner_classes:
      if target not in perturbs[exp_label]['0'][test['id']]:
        score = 0
        for predict in predicts:
          if predict['entity'].endswith(target):
            score -= predict['score']
        successful_attack = score < 0
        perturbs[exp_label]['0'][test['id']][target] = {
              'adv_example': input,
              'adv_example_enc': [],
              'input': input,
              'golden_labels': labels,
              'adv_predictions': predicts,
              'input_prediction': predicts,
              'adv_target_success': successful_attack,
              'target_label': target,
              'adv_generation_time': 0,
              'budget': 0,
              'maxiter': maxiter,
              'popsize': popsize
            }
  with tqdm(total=len(inputs)*(max_budget-min_budget+1)*len(ner_classes), desc="Adv. Examples") as pbar:
    for budget in range(min_budget, max_budget+1):
      if str(budget) not in perturbs[exp_label]:
        perturbs[exp_label][str(budget)] = dict()
      for input in inputs:
        if input['id'] not in perturbs[exp_label][str(budget)]:
          perturbs[exp_label][str(budget)][input['id']] = dict()
        for target in ner_classes:
          if target not in perturbs[exp_label][str(budget)][input['id']]:
            if NLP_DEFENCE:
              sentence = defence(detokenize(input['tokens']))
            else:
              sentence = detokenize(input['tokens'])
            obj = objective(model, sentence, ner_tags(input['ner_tags']), target, budget)
            example = obj.differential_evolution(verbose=False, maxiter=maxiter, popsize=popsize)
            perturbs[exp_label][str(budget)][input['id']][target] = example
            with open(file, 'wb') as f:
              pickle.dump(perturbs, f)
          sleep(0.1)
          pbar.update(1)

def emotion_targeted_experiment(objective, model, inputs, file, min_budget, max_budget, maxiter, popsize, exp_label, overwrite):
  if overwrite or not exists(file):
    perturbs = { exp_label: { '0': dict() } }
  else:
    with open(file, 'rb') as f:
      perturbs = pickle.load(f)
    if exp_label not in perturbs:
      perturbs[exp_label] = dict()
    if '0' not in perturbs[exp_label]:
      perturbs[exp_label]['0'] = dict()
  for idx, test in enumerate(inputs):
    input = test['text']
    predicts = model(input)[0]
    labels = emotion_classes[test['label']]
    if str(idx) not in perturbs[exp_label]['0']:
      perturbs[exp_label]['0'][str(idx)] = dict()
    for target, target_label in enumerate(emotion_classes):
      if target_label not in perturbs[exp_label]['0'][str(idx)]:
        score = -predicts[target]['score']
        if np.argmax(list(map(lambda x: x['score'], predicts))) == target:
          score -= 1
        successful_attack = score <= -1
        perturbs[exp_label]['0'][str(idx)][target_label] = {
              'adv_example': input,
              'adv_example_enc': [],
              'input': input,
              'golden_label': label,
              'adv_predictions': predicts,
              'input_prediction': predicts,
              'adv_target_success': successful_attack,
              'target_label': target_label,
              'target_index': target,
              'adv_generation_time': 0,
              'budget': 0,
              'maxiter': maxiter,
              'popsize': popsize
            }
  with tqdm(total=len(inputs)*(max_budget-min_budget+1)*len(emotion_classes), desc="Adv. Examples") as pbar:
    for budget in range(min_budget, max_budget+1):
      if str(budget) not in perturbs[exp_label]:
        perturbs[exp_label][str(budget)] = dict()
      for idx, input in enumerate(inputs):
        if str(idx) not in perturbs[exp_label][str(budget)]:
          perturbs[exp_label][str(budget)][str(idx)] = dict()
        for target, target_label in enumerate(emotion_classes):
          if target_label not in perturbs[exp_label][str(budget)][str(idx)]:
            sentence = input['text']
            obj = objective(model, sentence, emotion_classes[input['label']], target, budget)
            example = obj.differential_evolution(verbose=False, maxiter=maxiter, popsize=popsize)
            perturbs[exp_label][str(budget)][str(idx)][target_label] = example
            with open(file, 'wb') as f:
              pickle.dump(perturbs, f)
          else:
            # Required for progress bar to update correctly
            sleep(0.1)
          pbar.update(1)

def load_en2fr(cpu):
  # Load pre-trained translation model
  print("Loading EN->FR translation model.")
  getLogger('fairseq').setLevel(WARNING)
  en2fr = torch.hub.load('pytorch/fairseq',
                        'transformer.wmt14.en-fr',
                        tokenizer='moses',
                        bpe='subword_nmt',
                        verbose=False).eval()
  if cpu:
    en2fr.cpu()
  else:
    en2fr.cuda()
  print("Model loaded successfully.")
  return en2fr

def load_mnli(cpu):
  # Load pre-trained MNLI model
  print("Loading MNLI classification model.")
  getLogger('fairseq').setLevel(WARNING)
  mnli = torch.hub.load('pytorch/fairseq',
                        'roberta.large.mnli',
                        verbose=False).eval()
  if cpu:
    mnli.cpu()
  else:
    mnli.cuda()
  print("Model loaded successfully.")
  return mnli

def load_maxtoxic(cpu):
  getLogger().setLevel(WARNING)
  toxic = ModelWrapper()
  if not cpu:
    toxic.model.cuda()
    toxic.device = torch.device("cuda")
  return toxic

def load_perspective(api_key):
  return discovery.build(
    "commentanalyzer",
    "v1alpha1",
    developerKey=api_key,
    discoveryServiceUrl="https://commentanalyzer.googleapis.com/$discovery/rest?version=v1alpha1",
    cache=MemoryCache()
  )

def load_ner(cpu):
  return pipeline("ner", model="dbmdz/bert-large-cased-finetuned-conll03-english", device=(-1 if cpu else 0))

def load_emotion(cpu):
  return pipeline("text-classification", model='bhadresh-savani/distilbert-base-uncased-emotion', return_all_scores=True, device=(-1 if cpu else 0))

# -- CLI ---

if __name__ == '__main__':

  parser = ArgumentParser(description='Adversarial NLP Experiments.')
  technique = parser.add_mutually_exclusive_group(required=True)
  technique.add_argument('-i', '--invisible-chars', action='store_true', help="Use invisible character perturbations.")
  technique.add_argument('-g', '--homoglyphs', action='store_true', help="Use homoglyph perturbations.")
  technique.add_argument('-r', '--reorderings', action='store_true', help="Use reordering perturbations.")
  technique.add_argument('-d', '--deletions', action='store_true', help="Use deletion perturbations.")
  task = parser.add_mutually_exclusive_group(required=True)
  task.add_argument('-t', '--translation', action='store_true', help="Target translation task (EN->FR).")
  task.add_argument('-m', '--mnli', action='store_true', help="Target MNLI task (Roberta).")
  task.add_argument('-T', '--max-toxic', action='store_true', help="Target IBM Max Toxic model for toxic content.")
  task.add_argument('-P', '--perspective', action='store_true', help="Target Google Perspective API for toxic content.")
  task.add_argument('-N', '--ner', action='store_true', help="Target Huggingface NER model for entity recognition.")
  task.add_argument('-e', '--emotion', action='store_true', help="Target Huggingface emotion model for sentiment analysis.")
  parser.add_argument('-c', '--cpu', action='store_true', help="Use CPU for ML inference instead of CUDA.")
  parser.add_argument('pkl_file', help="File to contain Python pickled output.")
  parser.add_argument('-n', '--num-examples', type=int, default=500, help="Number of adversarial examples to generate.")
  parser.add_argument('-l', '--min-perturbs', type=int, default=1, help="The lower bound (inclusive) of the perturbation budget range.")
  parser.add_argument('-u', '--max-perturbs', type=int, default=5, help="The upper bound (inclusive) of the perturbation budget range.")
  parser.add_argument('-a', '--maxiter', type=int, default=10, help="The maximum number of iterations in the genetic algorithm.")
  parser.add_argument('-p', '--popsize', type=int, default=32, help="The size of the population in the genetic algorithm.")
  parser.add_argument('-o', '--overwrite', action='store_true', help="Overwrite existing results file instead of resuming.")
  parser.add_argument('-s', '--sponge', action='store_true', help="Perform an availability attack using sponge examples.")
  parser.add_argument('-R', '--rate-limit', type=int, default=10, help="The rate limit with which to throttle requests against the Google Perspective API (in QPS).")
  parser.add_argument('-D', '--nlp-defence', action='store_true', help="Use nlp defence.")
  targeted = parser.add_mutually_exclusive_group()
  targeted.add_argument('-x', '--targeted', action='store_true', help="Perform a targeted attack.")
  targeted.add_argument('-X', '--targeted-no-logits', action='store_true', help="Perform a targeted attack without access to inference result logits.")
  args = parser.parse_args()

  if args.nlp_defence:
    NLP_DEFENCE = True

  if args.translation:
    if args.targeted or args.targeted_no_logits:
      print("Targeted attacks for translation do not exist.")
      sys.exit(1)

    en2fr = load_en2fr(args.cpu)
    source, target, n_examples = load_source(args.num_examples)
    print(f"Loaded {n_examples} strings from corpus.")

    if args.sponge:
      if args.invisible_chars:
        print("Starting invisible character sponge example translation experiment.")
        objective = InvisibleCharacterSpongeObjective
        label = "translation_sponge_invisibles"
      elif args.homoglyphs:
        print("Starting homoglyph sponge example translation experiment.")
        objective = HomoglyphSpongeObjective
        label = "translation_sponge_homoglyphs"
      elif args.reorderings:
        print("Starting reordering sponge example translation experiment.")
        objective = ReorderSpongeObjective
        label = "translation_sponge_reorderings"
      elif args.deletions:
        print("Starting deletion sponge example translation experiment.")
        objective = DeletionSpongeObjective
        label = "translation_sponge_deletions"

      sponge_experiment(en2fr, objective, source, target, args.min_perturbs, args.max_perturbs, args.pkl_file, args.maxiter, args.popsize, n_examples, label, args.overwrite)

    else:
      if args.invisible_chars:
        print("Starting invisible characters translation experiment.")
        objective = InvisibleCharacterObjective
        label = "translation_invisibles"
      elif args.homoglyphs:
        print("Starting homoglyphs translation experiment.")
        objective = HomoglyphObjective
        label = "translation_homoglyphs"
      elif args.reorderings:
        print("Starting reorderings translation experiment.")
        objective = ReorderObjective
        label = "translation_reorderings"
      elif args.deletions:
        print("Starting deletions translation experiment.")
        objective = DeletionObjective
        label = "translation_deletions"

      experiment(en2fr, objective, source, target, args.min_perturbs, args.max_perturbs, args.pkl_file, args.maxiter, args.popsize, n_examples, label, args.overwrite)

  elif args.mnli:
    if args.sponge:
      print("Sponge example attacks for MNLI have not been implemented.")
      sys.exit(1)

    mnli = load_mnli(args.cpu)
    data = mnli_test[:args.num_examples]
    print(f"Loaded {len(data)} strings from corpus.")

    if args.targeted:
      if args.invisible_chars:
        print(f"Starting invisible characters targeted MNLI experiment.")
        objective = InvisibleCharacterTargetedMnliObjective
        label = "mnli_invisibles_targeted"
      elif args.homoglyphs:
        print(f"Starting homoglyphs targeted MNLI experiment.")
        objective = HomoglyphTargetedMnliObjective
        label = "mnli_homoglyphs_targeted"
      elif args.reorderings:
        print(f"Starting reorderings targeted MNLI experiment.")
        objective = ReorderTargetedMnliObjective
        label = "mnli_reorderings_targeted"
      elif args.deletions:
        print(f"Starting deletions targeted MNLI experiment.")
        objective = DeletionTargetedMnliObjective
        label = "mnli_deletions_targeted"
      
      mnli_targeted_experiment(objective, mnli, data, args.pkl_file, args.min_perturbs, args.max_perturbs, args.maxiter, args.popsize, label, args.overwrite)
    
    elif args.targeted_no_logits:
      if args.invisible_chars:
        print(f"Starting invisible characters targeted MNLI (no logits) experiment.")
        objective = InvisibleCharacterTargetedMnliNoLogitsObjective
        label = "mnli_invisibles_targeted_nologits"
      elif args.homoglyphs:
        print(f"Starting homoglyphs targeted MNLI (no logits) experiment.")
        objective = HomoglyphTargetedMnliNoLogitsObjective
        label = "mnli_homoglyphs_targeted_nologits"
      elif args.reorderings:
        print(f"Starting reorderings targeted MNLI (no logits) experiment.")
        objective = ReorderTargetedMnliNoLogitsObjective
        label = "mnli_reorderings_targeted_nologits"
      elif args.deletions:
        print(f"Starting deletions targeted MNLI (no logits) experiment.")
        objective = DeletionTargetedMnliNoLogitsObjective
        label = "mnli_deletions_targeted_nologits"
      
      mnli_targeted_experiment(objective, mnli, data, args.pkl_file, args.min_perturbs, args.max_perturbs, args.maxiter, args.popsize, label, args.overwrite)
    
    else:
      if args.invisible_chars:
        print(f"Starting invisible characters MNLI experiment.")
        objective = InvisibleCharacterMnliObjective
        label = "mnli_invisibles_untargeted"
      elif args.homoglyphs:
        print(f"Starting homoglyphs MNLI experiment.")
        objective = HomoglyphMnliObjective
        label = "mnli_homoglyphs_untargeted"
      elif args.reorderings:
        print(f"Starting reorderings MNLI experiment.")
        objective = ReorderMnliObjective
        label = "mnli_reorderings_untargeted"
      elif args.deletions:
        print(f"Starting deletions MNLI experiment.")
        objective = DeletionMnliObjective
        label = "mnli_deletions_untargeted"

      mnli_experiment(mnli, objective, data, args.pkl_file, args.min_perturbs, args.max_perturbs, args.maxiter, args.popsize, label, args.overwrite)

  elif args.max_toxic:
    if args.targeted or args.targeted_no_logits:
      print("Targeted attacks for Max Toxic have not been implemented.")
      sys.exit(1)
    elif args.sponge:
      print("Sponge example attacks for Max Toxic have not been implemented.")
      sys.exit(1)

    maxtoxic = load_maxtoxic(args.cpu)
    data = examples[:args.num_examples]
    print(f"Loaded {len(data)} strings from corpus.")

    if args.invisible_chars:
      print(f"Starting invisible characters Max Toxic experiment.")
      objective = InvisibleToxicObjective
      label = "maxtoxic_invisibles"
    elif args.homoglyphs:
      print(f"Starting homoglyphs Max Toxic experiment.")
      objective = HomoglyphToxicObjective
      label = "maxtoxic_homoglyphs"
    elif args.reorderings:
      print(f"Starting reorderings Max Toxic experiment.")
      objective = ReorderToxicObjective
      label = "maxtoxic_reorderings"
    elif args.deletions:
      print(f"Starting deletions Max Toxic experiment.")
      objective = DeletionToxicObjective
      label = "maxtoxic_deletions"

    max_toxic_experiment(objective, maxtoxic, args.pkl_file, args.min_perturbs, args.max_perturbs, data, args.maxiter, args.popsize, label, args.overwrite)

  elif args.perspective:
    if args.targeted or args.targeted_no_logits:
      print("Targeted attacks for the Perspective API have not been implemented.")
      sys.exit(1)
    elif args.sponge:
      print("Sponge example attacks for the Perspective API have not been implemented.")
      sys.exit(1)

    api_key = getpass("Perspective API Key: ")
    perspetive = load_perspective(api_key)
    data = examples[:args.num_examples]
    print(f"Loaded {len(data)} strings from corpus.")

    if args.invisible_chars:
      print(f"Starting invisible characters Perspective API experiment.")
      objective = InvisibleToxicPerspectiveObjective
      label = "perspective_invisibles"
    elif args.homoglyphs:
      print(f"Starting homoglyphs Perspective API experiment.")
      objective = HomoglyphToxicPerspectiveObjective
      label = "perspective_homoglyphs"
    elif args.reorderings:
      print(f"Starting reorderings Perspective API experiment.")
      objective = ReorderToxicPerspectiveObjective
      label = "perspective_reorderings"
    elif args.deletions:
      print(f"Starting deletions Perspective API experiment.")
      objective = DeletionToxicPerspectiveObjective
      label = "perspective_deletions"

    perspective_experiment(objective, perspetive, args.pkl_file, args.min_perturbs, args.max_perturbs, data, args.maxiter, args.popsize, label, args.overwrite, args.rate_limit)

  elif args.ner:
    if not args.targeted:
      print("Untargeted attacks for NER have not been implemented.")
      sys.exit(1)
    elif args.targeted_no_logits:
      print("No-logit targeted attacks for NER have not been implemented.")
      sys.exit(1)
    elif args.sponge:
      print("Sponge example attacks for NER have not been implemented.")
      sys.exit(1)

    ner = load_ner(args.cpu)
    ner_data = load_ner_data(args.num_examples)
    print(f"Loaded {len(ner_data)} strings from corpus.")

    if args.invisible_chars:
      print(f"Starting invisible characters NER experiment.")
      objective = InvisibleCharacterNerTargetedObjective
      label = "ner_targeted_invisibles"
    elif args.homoglyphs:
      print(f"Starting homoglyphs NER experiment.")
      objective = HomoglyphNerTargetedObjective
      label = "ner_targeted_homoglyphs"
    elif args.reorderings:
      print(f"Starting reorderings NER experiment.")
      objective = ReorderNerTargetedObjective
      label = "ner_targeted_reorderings"
    elif args.deletions:
      print(f"Starting deletions NER experiment.")
      objective = DeletionNerTargetedObjective
      label = "ner_targeted_deletions"

    ner_targeted_experiment(objective, ner, ner_data, args.pkl_file, args.min_perturbs, args.max_perturbs, args.maxiter, args.popsize, label, args.overwrite)

  
  elif args.emotion:
    if not args.targeted:
      print("Untargeted attacks for the emotion model have not been implemented.")
      sys.exit(1)
    elif args.targeted_no_logits:
      print("No-logit targeted attacks for the emotion model have not been implemented.")
      sys.exit(1)
    elif args.sponge:
      print("Sponge example attacks for the emotion model have not been implemented.")
      sys.exit(1)

    emotion = load_emotion(args.cpu)
    emotion_data = load_emotion_data(args.num_examples)
    print(f"Loaded {len(emotion_data)} strings from corpus.")

    if args.invisible_chars:
      print(f"Starting invisible characters emotion experiment.")
      objective = InvisibleCharacterEmotionTargetedObjective
      label = "emotion_targeted_invisibles"
    elif args.homoglyphs:
      print(f"Starting homoglyphs emotion experiment.")
      objective = HomoglyphEmotionTargetedObjective
      label = "emotion_targeted_homoglyphs"
    elif args.reorderings:
      print(f"Starting reorderings emotion experiment.")
      objective = ReorderEmotionTargetedObjective
      label = "emotion_targeted_reorderings"
    elif args.deletions:
      print(f"Starting deletions emotion experiment.")
      objective = DeletionEmotionTargetedObjective
      label = "emotion_targeted_deletions"

    emotion_targeted_experiment(objective, emotion, emotion_data, args.pkl_file, args.min_perturbs, args.max_perturbs, args.maxiter, args.popsize, label, args.overwrite)

print(f"Experiment complete. Results written to {args.pkl_file}.")
