from dataclasses import dataclass
from typing import List

@dataclass
class Entity:
    """ This dataclass mainly as a readme to show the data design, may never used in this code, because when read from 
    text, only the dictionary format is got from json.loads(str). It is not need to largely modify and debug these many
    codes to suit dataclass.
    :param
    form: the text content; 
    offset: the start index; 
    length: the form length;  

    used in 
        data_process/entity_label.py
        model/model_data_process/base_data_processor.py, get_entity_token_pos
    """
    form: str
    offset: int
    length: int
    type: str=''


@dataclass
class SplitText:
    text_id: str
    text: str
    entity_list: List[Entity]=[]
    distance_entity_list: List[Entity]=[]